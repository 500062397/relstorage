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
"""Blob management utilities needed by RelStorage.

Most of this code is lifted from ZODB/ZEO.
"""
from __future__ import absolute_import

import os
import threading
import time

import zc.lockfile
import ZODB.blob
from ZEO.ClientStorage import BlobCacheLayout
from ZEO.ClientStorage import _check_blob_cache_size
from ZODB import POSException
from ZODB.utils import p64
from ZODB.utils import u64

from relstorage._compat import iteritems


class BlobHelper(object):
    """Blob support for RelStorage.

    There is one BlobHelper per storage instance.  Each BlobHelper
    instance has access to the associated adapter as well as shared
    instances of fshelper (a ZODB.blob.FilesystemHelper) and
    cache_checker (a BlobCacheChecker).
    """

    # _txn_blobs: {oid->filename}; contains blob data for the
    # currently uncommitted transaction.
    _txn_blobs = None

    def __init__(self, options, adapter, fshelper=None, cache_checker=None):
        self.options = options
        self.adapter = adapter
        self.blob_dir = options.blob_dir
        self.shared_blob_dir = options.shared_blob_dir

        if fshelper is None:
            if self.shared_blob_dir:
                # Share files over NFS or similar
                fshelper = ZODB.blob.FilesystemHelper(self.blob_dir)
            else:
                # The blob directory is a cache of the blobs
                if 'zeocache' not in ZODB.blob.LAYOUTS:
                    ZODB.blob.LAYOUTS['zeocache'] = BlobCacheLayout()
                fshelper = ZODB.blob.FilesystemHelper(
                    self.blob_dir, layout_name='zeocache')
            fshelper.create()
        self.fshelper = fshelper

        if cache_checker is None:
            cache_checker = BlobCacheChecker(options)
        self.cache_checker = cache_checker

    def new_instance(self, adapter):
        return BlobHelper(options=self.options, adapter=adapter,
                          fshelper=self.fshelper, cache_checker=self.cache_checker)

    def clear_temp(self):
        self._txn_blobs = None

    @property
    def txn_has_blobs(self):
        return bool(self._txn_blobs)

    def close(self):
        self.cache_checker.close()

    def download_blob(self, cursor, oid, serial, filename):
        """Download a blob into a file"""
        tmp_fn = filename + ".tmp"
        bytecount = self.adapter.mover.download_blob(
            cursor, u64(oid), u64(serial), tmp_fn)
        if os.path.exists(tmp_fn):
            os.rename(tmp_fn, filename)
        self.cache_checker.loaded(bytecount)

    def upload_blob(self, cursor, oid, serial, filename):
        """Upload a blob from a file.

        If serial is None, upload to the temporary table.
        """
        if serial is not None:
            tid_int = u64(serial)
        else:
            tid_int = None
        self.adapter.mover.upload_blob(cursor, u64(oid), tid_int, filename)

    def _get_lockable_blob_filename(self, oid, serial):
        # Create the directory if needed
        self.fshelper.getPathForOID(oid, True)
        return self.fshelper.getBlobFilename(oid, serial)

    def loadBlob(self, cursor, oid, serial):
        blob_filename = self._get_lockable_blob_filename(oid, serial)
        lock = _lock_blob(blob_filename)
        try:
            return self._loadBlobLocked(cursor, oid, serial, blob_filename)
        finally:
            lock.close()

    def _loadBlobLocked(self, cursor, oid, serial, blob_filename):
        # Load a blob. If it isn't present and we have a shared blob
        # directory, then assume that it doesn't exist on the server
        # and return None.
        # Note that the thread that cleans the blob cache up when it reaches
        # a maximum size could remove the blob file by the time the caller
        # gets the filename, so it could be gone.
        if os.path.exists(blob_filename):
            return _accessed(blob_filename)

        if self.shared_blob_dir:
            # All the blobs are in a shared directory. If the file
            # isn't here, it's not anywhere.
            raise POSException.POSKeyError("No blob file", oid, serial)

        # First, we'll create the directory for this oid, if it doesn't exist.
        self.fshelper.getPathForOID(oid, create=True)

        # OK, it's not here and we (or someone) needs to get it. We
        # want to avoid getting it multiple times. We want to avoid
        # getting it multiple times even accross separate client
        # processes on the same machine. We'll use file locking.
        # (accomplished by our caller.)

        # We got the lock, so it's our job to download it. First,
        # we'll double check that someone didn't download it while
        # we were getting the lock:

        if os.path.exists(blob_filename):
            return _accessed(blob_filename)

        self.download_blob(cursor, oid, serial, blob_filename)

        if os.path.exists(blob_filename):
            return _accessed(blob_filename)

        raise POSException.POSKeyError("No blob file", oid, serial)

    def openCommittedBlobFile(self, cursor, oid, serial, blob=None):
        # First, try to make sure the file exists on disk.
        blob_filename = self._get_lockable_blob_filename(oid, serial)
        # Next, open and return it. This would be expected to either
        # return a file we can read, or raise a FileNotFoundError.
        # Sadly, on some platforms (macOS 10.14.5 with APFS on Python
        # 3), this has a race condition with a concurrent unlink
        # syscall from the cache cleaner, such that the open succeeds,
        # but reading fails to return any data, depending on how the
        # open and unlink syscalls are interleaved. So we must be sure
        # to prevent the two from overlapping; we do that by holding
        # the lock. See https://github.com/zodb/relstorage/issues/219
        #
        # Unfortunately, but not unexpectedly, this about doubles the amount
        # of time it takes to open blobs that are already present.
        lock = _lock_blob(blob_filename)
        try:
            self._loadBlobLocked(cursor, oid, serial, blob_filename)
            if blob is None:
                return open(blob_filename, 'rb')
            return ZODB.blob.BlobFile(blob_filename, 'r', blob)
        finally:
            lock.close()

    def temporaryDirectory(self):
        return self.fshelper.temp_dir

    def storeBlob(self, cursor, store_func,
                  oid, serial, data, blobfilename, version, txn):
        """Storage API: store a blob object."""
        assert not version

        # Grab the file right away. That way, if we don't have enough
        # room for a copy, we'll know now rather than in tpc_finish.
        # Also, this relieves the client of having to manage the file
        # (or the directory contianing it).
        self.fshelper.getPathForOID(oid, create=True)
        fd, target = self.fshelper.blob_mkstemp(oid, serial)
        os.close(fd)

        # It's a bit odd (and impossible on windows) to rename over an
        # existing file. We'll use the temporary file name as a base.
        target += '-'
        ZODB.blob.rename_or_copy_blob(blobfilename, target)
        os.remove(target[:-1])
        self._add_blob_to_transaction(oid, target)

        store_func(oid, serial, data, '', txn)

        if not self.shared_blob_dir:
            self.upload_blob(cursor, oid, None, target)

    def _add_blob_to_transaction(self, oid, filename):
        if self._txn_blobs is None:
            self._txn_blobs = {}
        else:
            old_filename = self._txn_blobs.get(oid)
            if old_filename is not None and old_filename != filename:
                ZODB.blob.remove_committed(old_filename)
        self._txn_blobs[oid] = filename

    def restoreBlob(self, cursor, oid, serial, blobfilename):
        if self.shared_blob_dir:
            self.fshelper.getPathForOID(oid, create=True)
            targetname = self.fshelper.getBlobFilename(oid, serial)
            ZODB.blob.rename_or_copy_blob(blobfilename, targetname)
        else:
            self.upload_blob(cursor, oid, serial, blobfilename)

    def copy_undone(self, copied, tid):
        """After an undo operation, copy the matching blobs forward.

        The copied parameter is a list of (integer oid, integer tid).
        """
        if not self.shared_blob_dir:
            # Not necessary
            return

        for oid_int, old_tid_int in copied:
            oid = p64(oid_int)
            old_tid = p64(old_tid_int)
            orig_fn = self.fshelper.getBlobFilename(oid, old_tid)
            if not os.path.exists(orig_fn):
                # not a blob
                continue

            new_fn = self.fshelper.getBlobFilename(oid, tid)
            with open(orig_fn, 'rb') as orig, open(new_fn, 'wb') as new:
                ZODB.utils.cp(orig, new)

            self._add_blob_to_transaction(oid, new_fn)

    def after_pack(self, oid_int, tid_int):
        """Called after an object state has been removed by packing.

        Removes the corresponding blob file.
        """
        if not self.shared_blob_dir:
            # Not necessary
            return

        oid = p64(oid_int)
        tid = p64(tid_int)
        fn = self.fshelper.getBlobFilename(oid, tid)
        if self.adapter.keep_history:
            # remove only the revision just packed
            if os.path.exists(fn):
                ZODB.blob.remove_committed(fn)
                dirname = os.path.dirname(fn)
                if not _has_files(dirname):
                    ZODB.blob.remove_committed_dir(dirname)
        else:
            # remove all revisions
            dirname = os.path.dirname(fn)
            if os.path.exists(dirname):
                for name in os.listdir(dirname):
                    ZODB.blob.remove_committed(os.path.join(dirname, name))
                ZODB.blob.remove_committed_dir(dirname)

    def vote(self, tid):
        if self._txn_blobs:
            # We now have a transaction ID, so rename all the blobs
            # accordingly.
            for oid, sourcename in self._txn_blobs.items():
                bytes = os.stat(sourcename).st_size
                self.cache_checker.loaded(bytes, check=False)
                targetname = self.fshelper.getBlobFilename(oid, tid)
                if sourcename != targetname:
                    lock = _lock_blob(targetname)
                    try:
                        ZODB.blob.rename_or_copy_blob(sourcename, targetname)
                    finally:
                        lock.close()
                    self._txn_blobs[oid] = targetname
            self.cache_checker.check(True)

    def abort(self):
        if self._txn_blobs:
            for _oid, filename in iteritems(self._txn_blobs):
                if os.path.exists(filename):
                    ZODB.blob.remove_committed(filename)
                    if self.shared_blob_dir:
                        dirname = os.path.dirname(filename)
                        if not _has_files(dirname):
                            ZODB.blob.remove_committed_dir(dirname)


class BlobCacheChecker(object):
    """Control the size of the blob cache.  Shared between BlobHelpers."""

    def __init__(self, options):
        self.blob_dir = options.blob_dir
        self.shared_blob_dir = options.shared_blob_dir
        self._blob_cache_size = options.blob_cache_size
        self._blob_data_bytes_loaded = 0
        if self._blob_cache_size is not None:
            assert options.blob_cache_size_check < 100
            self._blob_cache_size_check = (
                self._blob_cache_size * options.blob_cache_size_check / 100)
            self.check()

    def close(self):
        if self._check_blob_size_thread is not None:
            self._check_blob_size_thread.join()

    def loaded(self, bytes, check=True):
        self._blob_data_bytes_loaded += bytes
        if check:
            self.check(True)

    _check_blob_size_thread = None

    def check(self, check_loaded=False):
        """If appropriate, run blob cache cleanup in another thread."""
        if self._blob_cache_size is None:
            return
        if self.shared_blob_dir or not self.blob_dir:
            return

        if (check_loaded and
                self._blob_data_bytes_loaded < self._blob_cache_size_check):
            return

        self._blob_data_bytes_loaded = 0

        target = max(self._blob_cache_size - self._blob_cache_size_check, 0)

        check_blob_size_thread = threading.Thread(
            target=_check_blob_cache_size,
            args=(self.blob_dir, target),
        )
        check_blob_size_thread.setDaemon(True)
        check_blob_size_thread.start()
        self._check_blob_size_thread = check_blob_size_thread


def _has_files(dirname):
    """Return True if a directory has any visible files."""
    names = os.listdir(dirname)
    if not names:
        return False
    for name in names:
        if not name.startswith('.'):
            return True
    return False


# Note: the following code is copied directly from ZEO.ClientStorage.
# Because the symbols are not public (the function names start with an
# underscore), indicating their signature could change at any time.

def _accessed(filename):
    try:
        os.utime(filename, (time.time(), os.stat(filename).st_mtime))
    except OSError:
        pass # We tried. :)
    return filename

def _lock_blob(path):
    lockfilename = os.path.join(os.path.dirname(path), '.lock')
    n = 0
    while 1:
        try:
            return zc.lockfile.LockFile(lockfilename)
        except zc.lockfile.LockError:
            time.sleep(0.01)
            n += 1
            if n > 60000:
                raise
        else:
            break
