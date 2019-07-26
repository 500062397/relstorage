##############################################################################
#
# Copyright (c) 2009 Zope Foundation and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE.
#
##############################################################################
"""
Database schema installers
"""
from __future__ import absolute_import
from __future__ import print_function

from zope.interface import implementer

from ..interfaces import ISchemaInstaller
from ..schema import AbstractSchemaInstaller

logger = __import__('logging').getLogger(__name__)


@implementer(ISchemaInstaller)
class PostgreSQLSchemaInstaller(AbstractSchemaInstaller):

    database_type = 'postgresql'

    _PROCEDURES = {} # Caching of proc files.

    def __init__(self, options, connmanager, runner, locker):
        self.options = options
        super(PostgreSQLSchemaInstaller, self).__init__(
            connmanager, runner, options.keep_history)
        self.locker = locker

    def get_database_name(self, cursor):
        cursor.execute("SELECT current_database()")
        row, = cursor.fetchall()
        name, = row
        return self._metadata_to_native_str(name)

    def _prepare_with_connection(self, conn, cursor):
        super(PostgreSQLSchemaInstaller, self)._prepare_with_connection(conn, cursor)

        if not self.all_procedures_installed(cursor):
            self.install_procedures(cursor)
            if not self.all_procedures_installed(cursor):
                raise AssertionError(
                    "Could not get version information after "
                    "installing the stored procedures.")

        triggers = self.list_triggers(cursor)
        if 'blob_chunk_delete' not in triggers:
            self.install_triggers(cursor)

        # Do we need to merge blob chunks?
        if not self.options.shared_blob_dir:
            cursor.execute('SELECT chunk_num FROM blob_chunk WHERE chunk_num > 0 LIMIT 1')
            if cursor.fetchone():
                logger.info("Merging blob chunks on the server.")
                cursor.execute("SELECT merge_blob_chunks()")
                # If we've done our job right, any blobs cached on
                # disk are still perfectly valid.

    def __native_names_only(self, cursor):
        native = self._metadata_to_native_str
        return [
            native(name)
            for (name,) in cursor.fetchall()
        ]

    def list_tables(self, cursor):
        cursor.execute("SELECT tablename FROM pg_tables")
        return self.__native_names_only(cursor)

    def list_sequences(self, cursor):
        cursor.execute("SELECT relname FROM pg_class WHERE relkind = 'S'")
        return self.__native_names_only(cursor)

    def list_languages(self, cursor):
        cursor.execute("SELECT lanname FROM pg_catalog.pg_language")
        return self.__native_names_only(cursor)

    def install_languages(self, cursor):
        if 'plpgsql' not in self.list_languages(cursor):
            cursor.execute("CREATE LANGUAGE plpgsql")

    def list_procedures(self, cursor):
        """
        Returns {procedure name: checksum}. *checksum* may be None.
        """
        # The description is populated with ``COMMENT ON FUNCTION <name> IS 'comment'``
        stmt = """
        SELECT p.proname AS funcname,  d.description
        FROM pg_proc p
        INNER JOIN pg_namespace n ON n.oid = p.pronamespace
        LEFT JOIN pg_description As d ON (d.objoid = p.oid)
        WHERE n.nspname = 'public'
        """
        cursor.execute(stmt)
        res = {}
        native = self._metadata_to_native_str
        for (name, checksum) in cursor.fetchall():
            name = native(name)
            checksum = native(checksum) if checksum is not None else None
            res[name.lower()] = checksum
        return res

    def all_procedures_installed(self, cursor):
        """
        Check whether all required stored procedures are installed.

        Returns True only if all required procedures are installed and
        up to date.
        """

        expected = {
            proc_name: self._checksum_for_str(proc_source)
            for proc_name, proc_source
            in self.procedures.items()
        }

        installed = self.list_procedures(cursor)
        if installed != expected:
            logger.info(
                "Procedures incorrect, will reinstall. "
                "Expected: %s."
                "Actual: %s",
                expected, installed
            )
            return False
        return True

    def install_procedures(self, cursor):
        """Install the stored procedures"""
        self.install_languages(cursor)
        # PostgreSQL procedures in the SQL language
        # do lots of validation at compile time; in particular,
        # they check that the functions they use in SELECT statements
        # actually exist. When we have procedures that call each other,
        # that means there's an order they have to be created in.
        # Rather than try to figure out what that order is, or encode it
        # in names somehow, we just loop multiple times until we don't get any errors,
        # figuring that we'll create a leaf function on the first time, and then
        # more and more dependent functions on each iteration.
        iters = range(5)
        last_ex = None
        for _ in iters:
            last_ex = None
            for proc_name, proc_source in self.procedures.items():
                __traceback_info__ = proc_name, self.keep_history
                checksum = self._checksum_for_str(proc_source)

                try:
                    cursor.execute(proc_source)
                except self.connmanager.driver.driver_module.ProgrammingError as ex:
                    logger.info("Failed to create %s: %s", proc_name, ex)
                    last_ex = ex, __traceback_info__
                    cursor.connection.rollback()
                    continue

                # For pg8000 we can't use a parameter here.
                comment = "COMMENT ON FUNCTION %s IS '%s'" % (proc_name, checksum)
                cursor.execute(comment)
                cursor.connection.commit()

            if last_ex is None:
                return

        last_ex, __traceback_info__ = last_ex
        raise last_ex

    def list_triggers(self, cursor):
        cursor.execute("SELECT tgname FROM pg_trigger")
        return self.__native_names_only(cursor)

    def install_triggers(self, cursor):
        stmt = """
        CREATE TRIGGER blob_chunk_delete
            BEFORE DELETE ON blob_chunk
            FOR EACH ROW
            EXECUTE PROCEDURE blob_chunk_delete_trigger()
        """
        cursor.execute(stmt)

    def drop_all(self):
        def callback(_conn, cursor):
            if 'blob_chunk' in self.list_tables(cursor):
                # Trigger deletion of blob OIDs.
                cursor.execute("DELETE FROM blob_chunk")
        self.connmanager.open_and_call(callback)
        super(PostgreSQLSchemaInstaller, self).drop_all()

    def _create_pack_lock(self, cursor):
        return

    def _create_new_oid(self, cursor):
        stmt = """
        CREATE SEQUENCE IF NOT EXISTS zoid_seq;
        """
        self.runner.run_script(cursor, stmt)


    CREATE_PACK_OBJECT_IX_TMPL = """
    CREATE INDEX pack_object_keep_false ON pack_object (zoid)
        WHERE keep = false;
    CREATE INDEX pack_object_keep_true ON pack_object (visited)
        WHERE keep = true;
    """

    def _reset_oid(self, cursor):
        stmt = "ALTER SEQUENCE zoid_seq RESTART WITH 1;"
        self.runner.run_script(cursor, stmt)

    # Use the fast, semi-transactional way to truncate tables. It's
    # not MVCC safe, but "TRUNCATE is transaction-safe with respect to
    # the data in the tables: the truncation will be safely rolled
    # back if the surrounding transaction does not commit."
    _zap_all_tbl_stmt = 'TRUNCATE TABLE %s CASCADE'

    def _before_zap_all_tables(self, cursor, tables, slow=False):
        super(PostgreSQLSchemaInstaller, self)._before_zap_all_tables(cursor, tables, slow)
        if not slow and 'blob_chunk' in tables:
            # If we're going to be truncating, it's important to
            # remove the large objects through lo_unlink. We have a
            # trigger that does that, but only for DELETE.
            # The `vacuumlo` command cleans up any that might have been
            # missed.

            # This unfortunately results in returning a row for each
            # object unlinked, but it should still be faster than
            # running a DELETE and firing the trigger for each row.
            cursor.execute("""
            SELECT lo_unlink(t.chunk)
            FROM
            (SELECT DISTINCT chunk FROM blob_chunk)
            AS t
            """)
