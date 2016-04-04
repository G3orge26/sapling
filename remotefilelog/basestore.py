import os, shutil, time
import ioutil
from mercurial import util
from mercurial.i18n import _
from mercurial.node import hex

class basestore(object):
    def __init__(self, ui, path, reponame, shared=False):
        path = util.expandpath(path)
        self.ui = ui
        self._path = path
        self._reponame = reponame
        self._shared = shared
        self._uid = os.getuid()
        self._fetches = []

        if shared:
            if not os.path.exists(path):
                oldumask = os.umask(0o002)
                try:
                    os.makedirs(path)

                    groupname = self.ui.config("remotefilelog", "cachegroup")
                    if groupname:
                        gid = grp.getgrnam(groupname).gr_gid
                        if gid:
                            os.chown(cachepath, os.getuid(), gid)
                            os.chmod(cachepath, 0o2775)
                finally:
                    os.umask(oldumask)

    def addfetcher(self, fetchfunc):
        self._fetches.append(fetchfunc)

    def triggerfetches(self, keys):
        for fetcher in self._fetches:
            fetcher(keys)

    def contains(self, keys):
        missing = []
        for name, node in keys:
            filepath = self._getfilepath(name, node)
            exists = os.path.exists(filepath)
            if not exists:
                missing.append((name, node))

        return missing

    # BELOW THIS ARE NON-STANDARD APIS

    def _getfilepath(self, name, node):
        node = hex(node)
        if self._shared:
            key = ioutil.getcachekey(self._reponame, name, node)
        else:
            key = ioutil.getlocalkey(name, node)

        return os.path.join(self._path, key)

