# match.py - filename matching
#
#  Copyright 2008, 2009 Matt Mackall <mpm@selenic.com> and others
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2 or any later version.

from __future__ import absolute_import

import copy
import os
import re

from .i18n import _
from . import (
    error,
    pathutil,
    util,
)

propertycache = util.propertycache

def _rematcher(regex):
    '''compile the regexp with the best available regexp engine and return a
    matcher function'''
    m = util.re.compile(regex)
    try:
        # slightly faster, provided by facebook's re2 bindings
        return m.test_match
    except AttributeError:
        return m.match

def _expandsets(kindpats, ctx, listsubrepos):
    '''Returns the kindpats list with the 'set' patterns expanded.'''
    fset = set()
    other = []

    for kind, pat, source in kindpats:
        if kind == 'set':
            if not ctx:
                raise error.Abort(_("fileset expression with no context"))
            s = ctx.getfileset(pat)
            fset.update(s)

            if listsubrepos:
                for subpath in ctx.substate:
                    s = ctx.sub(subpath).getfileset(pat)
                    fset.update(subpath + '/' + f for f in s)

            continue
        other.append((kind, pat, source))
    return fset, other

def _expandsubinclude(kindpats, root):
    '''Returns the list of subinclude matcher args and the kindpats without the
    subincludes in it.'''
    relmatchers = []
    other = []

    for kind, pat, source in kindpats:
        if kind == 'subinclude':
            sourceroot = pathutil.dirname(util.normpath(source))
            pat = util.pconvert(pat)
            path = pathutil.join(sourceroot, pat)

            newroot = pathutil.dirname(path)
            matcherargs = (newroot, '', [], ['include:%s' % path])

            prefix = pathutil.canonpath(root, root, newroot)
            if prefix:
                prefix += '/'
            relmatchers.append((prefix, matcherargs))
        else:
            other.append((kind, pat, source))

    return relmatchers, other

def _kindpatsalwaysmatch(kindpats):
    """"Checks whether the kindspats match everything, as e.g.
    'relpath:.' does.
    """
    for kind, pat, source in kindpats:
        if pat != '' or kind not in ['relpath', 'glob']:
            return False
    return True

def match(root, cwd, patterns, include=None, exclude=None, default='glob',
          exact=False, auditor=None, ctx=None, listsubrepos=False, warn=None,
          badfn=None, icasefs=False):
    """build an object to match a set of file patterns

    arguments:
    root - the canonical root of the tree you're matching against
    cwd - the current working directory, if relevant
    patterns - patterns to find
    include - patterns to include (unless they are excluded)
    exclude - patterns to exclude (even if they are included)
    default - if a pattern in patterns has no explicit type, assume this one
    exact - patterns are actually filenames (include/exclude still apply)
    warn - optional function used for printing warnings
    badfn - optional bad() callback for this matcher instead of the default
    icasefs - make a matcher for wdir on case insensitive filesystems, which
        normalizes the given patterns to the case in the filesystem

    a pattern is one of:
    'glob:<glob>' - a glob relative to cwd
    're:<regexp>' - a regular expression
    'path:<path>' - a path relative to repository root, which is matched
                    recursively
    'rootfilesin:<path>' - a path relative to repository root, which is
                    matched non-recursively (will not match subdirectories)
    'relglob:<glob>' - an unrooted glob (*.c matches C files in all dirs)
    'relpath:<path>' - a path relative to cwd
    'relre:<regexp>' - a regexp that needn't match the start of a name
    'set:<fileset>' - a fileset expression
    'include:<path>' - a file of patterns to read and include
    'subinclude:<path>' - a file of patterns to match against files under
                          the same directory
    '<something>' - a pattern of the specified default type
    """
    normalize = _donormalize
    if icasefs:
        dirstate = ctx.repo().dirstate
        dsnormalize = dirstate.normalize

        def normalize(patterns, default, root, cwd, auditor, warn):
            kp = _donormalize(patterns, default, root, cwd, auditor, warn)
            kindpats = []
            for kind, pats, source in kp:
                if kind not in ('re', 'relre'):  # regex can't be normalized
                    p = pats
                    pats = dsnormalize(pats)

                    # Preserve the original to handle a case only rename.
                    if p != pats and p in dirstate:
                        kindpats.append((kind, p, source))

                kindpats.append((kind, pats, source))
            return kindpats

    return matcher(root, cwd, normalize, patterns, include=include,
                   exclude=exclude, default=default, exact=exact,
                   auditor=auditor, ctx=ctx, listsubrepos=listsubrepos,
                   warn=warn, badfn=badfn)

def exact(root, cwd, files, badfn=None):
    return match(root, cwd, files, exact=True, badfn=badfn)

def always(root, cwd):
    return match(root, cwd, [])

def badmatch(match, badfn):
    """Make a copy of the given matcher, replacing its bad method with the given
    one.
    """
    m = copy.copy(match)
    m.bad = badfn
    return m

def _donormalize(patterns, default, root, cwd, auditor, warn):
    '''Convert 'kind:pat' from the patterns list to tuples with kind and
    normalized and rooted patterns and with listfiles expanded.'''
    kindpats = []
    for kind, pat in [_patsplit(p, default) for p in patterns]:
        if kind in ('glob', 'relpath'):
            pat = pathutil.canonpath(root, cwd, pat, auditor)
        elif kind in ('relglob', 'path', 'rootfilesin'):
            pat = util.normpath(pat)
        elif kind in ('listfile', 'listfile0'):
            try:
                files = util.readfile(pat)
                if kind == 'listfile0':
                    files = files.split('\0')
                else:
                    files = files.splitlines()
                files = [f for f in files if f]
            except EnvironmentError:
                raise error.Abort(_("unable to read file list (%s)") % pat)
            for k, p, source in _donormalize(files, default, root, cwd,
                                             auditor, warn):
                kindpats.append((k, p, pat))
            continue
        elif kind == 'include':
            try:
                fullpath = os.path.join(root, util.localpath(pat))
                includepats = readpatternfile(fullpath, warn)
                for k, p, source in _donormalize(includepats, default,
                                                 root, cwd, auditor, warn):
                    kindpats.append((k, p, source or pat))
            except error.Abort as inst:
                raise error.Abort('%s: %s' % (pat, inst[0]))
            except IOError as inst:
                if warn:
                    warn(_("skipping unreadable pattern file '%s': %s\n") %
                         (pat, inst.strerror))
            continue
        # else: re or relre - which cannot be normalized
        kindpats.append((kind, pat, ''))
    return kindpats

class matcher(object):

    def __init__(self, root, cwd, normalize, patterns, include=None,
                 exclude=None, default='glob', exact=False, auditor=None,
                 ctx=None, listsubrepos=False, warn=None, badfn=None):
        if include is None:
            include = []
        if exclude is None:
            exclude = []

        self._root = root
        self._cwd = cwd
        self._files = [] # exact files and roots of patterns
        self._anypats = bool(include or exclude)
        self._always = False
        self._pathrestricted = bool(include or exclude or patterns)

        # roots are directories which are recursively included/excluded.
        self._includeroots = set()
        self._excluderoots = set()
        # dirs are directories which are non-recursively included.
        self._includedirs = set()

        if badfn is not None:
            self.bad = badfn

        matchfns = []
        if include:
            kindpats = normalize(include, 'glob', root, cwd, auditor, warn)
            self.includepat, im = _buildmatch(ctx, kindpats, '(?:/|$)',
                                              listsubrepos, root)
            roots, dirs = _rootsanddirs(kindpats)
            self._includeroots.update(roots)
            self._includedirs.update(dirs)
            matchfns.append(im)
        if exclude:
            kindpats = normalize(exclude, 'glob', root, cwd, auditor, warn)
            self.excludepat, em = _buildmatch(ctx, kindpats, '(?:/|$)',
                                              listsubrepos, root)
            if not _anypats(kindpats):
                # Only consider recursive excludes as such - if a non-recursive
                # exclude is used, we must still recurse into the excluded
                # directory, at least to find subdirectories. In such a case,
                # the regex still won't match the non-recursively-excluded
                # files.
                self._excluderoots.update(_roots(kindpats))
            matchfns.append(lambda f: not em(f))
        if exact:
            if isinstance(patterns, list):
                self._files = patterns
            else:
                self._files = list(patterns)
            matchfns.append(self.exact)
        elif patterns:
            kindpats = normalize(patterns, default, root, cwd, auditor, warn)
            if not _kindpatsalwaysmatch(kindpats):
                self._files = _explicitfiles(kindpats)
                self._anypats = self._anypats or _anypats(kindpats)
                self.patternspat, pm = _buildmatch(ctx, kindpats, '$',
                                                   listsubrepos, root)
                matchfns.append(pm)

        if not matchfns:
            m = util.always
            self._always = True
        elif len(matchfns) == 1:
            m = matchfns[0]
        else:
            def m(f):
                for matchfn in matchfns:
                    if not matchfn(f):
                        return False
                return True

        self.matchfn = m

    def __call__(self, fn):
        return self.matchfn(fn)
    def __iter__(self):
        for f in self._files:
            yield f

    # Callbacks related to how the matcher is used by dirstate.walk.
    # Subscribers to these events must monkeypatch the matcher object.
    def bad(self, f, msg):
        '''Callback from dirstate.walk for each explicit file that can't be
        found/accessed, with an error message.'''
        pass

    # If an explicitdir is set, it will be called when an explicitly listed
    # directory is visited.
    explicitdir = None

    # If an traversedir is set, it will be called when a directory discovered
    # by recursive traversal is visited.
    traversedir = None

    def abs(self, f):
        '''Convert a repo path back to path that is relative to the root of the
        matcher.'''
        return f

    def rel(self, f):
        '''Convert repo path back to path that is relative to cwd of matcher.'''
        return util.pathto(self._root, self._cwd, f)

    def uipath(self, f):
        '''Convert repo path to a display path.  If patterns or -I/-X were used
        to create this matcher, the display path will be relative to cwd.
        Otherwise it is relative to the root of the repo.'''
        return (self._pathrestricted and self.rel(f)) or self.abs(f)

    def files(self):
        '''Explicitly listed files or patterns or roots:
        if no patterns or .always(): empty list,
        if exact: list exact files,
        if not .anypats(): list all files and dirs,
        else: optimal roots'''
        return self._files

    @propertycache
    def _fileset(self):
        return set(self._files)

    @propertycache
    def _dirs(self):
        return set(util.dirs(self._fileset)) | {'.'}

    def visitdir(self, dir):
        '''Decides whether a directory should be visited based on whether it
        has potential matches in it or one of its subdirectories. This is
        based on the match's primary, included, and excluded patterns.

        Returns the string 'all' if the given directory and all subdirectories
        should be visited. Otherwise returns True or False indicating whether
        the given directory should be visited.

        This function's behavior is undefined if it has returned False for
        one of the dir's parent directories.
        '''
        if self.prefix() and dir in self._fileset:
            return 'all'
        if dir in self._excluderoots:
            return False
        if ((self._includeroots or self._includedirs) and
            '.' not in self._includeroots and
            dir not in self._includeroots and
            dir not in self._includedirs and
            not any(parent in self._includeroots
                    for parent in util.finddirs(dir))):
            return False
        return (not self._fileset or
                '.' in self._fileset or
                dir in self._fileset or
                dir in self._dirs or
                any(parentdir in self._fileset
                    for parentdir in util.finddirs(dir)))

    def exact(self, f):
        '''Returns True if f is in .files().'''
        return f in self._fileset

    def anypats(self):
        '''Matcher uses patterns or include/exclude.'''
        return self._anypats

    def always(self):
        '''Matcher will match everything and .files() will be empty
        - optimization might be possible and necessary.'''
        return self._always

    def isexact(self):
        return self.matchfn == self.exact

    def prefix(self):
        return not self.always() and not self.isexact() and not self.anypats()

class subdirmatcher(matcher):
    """Adapt a matcher to work on a subdirectory only.

    The paths are remapped to remove/insert the path as needed:

    >>> m1 = match('root', '', ['a.txt', 'sub/b.txt'])
    >>> m2 = subdirmatcher('sub', m1)
    >>> bool(m2('a.txt'))
    False
    >>> bool(m2('b.txt'))
    True
    >>> bool(m2.matchfn('a.txt'))
    False
    >>> bool(m2.matchfn('b.txt'))
    True
    >>> m2.files()
    ['b.txt']
    >>> m2.exact('b.txt')
    True
    >>> util.pconvert(m2.rel('b.txt'))
    'sub/b.txt'
    >>> def bad(f, msg):
    ...     print "%s: %s" % (f, msg)
    >>> m1.bad = bad
    >>> m2.bad('x.txt', 'No such file')
    sub/x.txt: No such file
    >>> m2.abs('c.txt')
    'sub/c.txt'
    """

    def __init__(self, path, matcher):
        self._root = matcher._root
        self._cwd = matcher._cwd
        self._path = path
        self._matcher = matcher
        self._always = matcher._always

        self._files = [f[len(path) + 1:] for f in matcher._files
                       if f.startswith(path + "/")]

        # If the parent repo had a path to this subrepo and the matcher is
        # a prefix matcher, this submatcher always matches.
        if matcher.prefix():
            self._always = any(f == path for f in matcher._files)

        self._anypats = matcher._anypats
        # Some information is lost in the superclass's constructor, so we
        # can not accurately create the matching function for the subdirectory
        # from the inputs. Instead, we override matchfn() and visitdir() to
        # call the original matcher with the subdirectory path prepended.
        self.matchfn = lambda fn: matcher.matchfn(self._path + "/" + fn)

    def bad(self, f, msg):
        self._matcher.bad(self._path + "/" + f, msg)

    def abs(self, f):
        return self._matcher.abs(self._path + "/" + f)

    def rel(self, f):
        return self._matcher.rel(self._path + "/" + f)

    def uipath(self, f):
        return self._matcher.uipath(self._path + "/" + f)

    def visitdir(self, dir):
        if dir == '.':
            dir = self._path
        else:
            dir = self._path + "/" + dir
        return self._matcher.visitdir(dir)

def patkind(pattern, default=None):
    '''If pattern is 'kind:pat' with a known kind, return kind.'''
    return _patsplit(pattern, default)[0]

def _patsplit(pattern, default):
    """Split a string into the optional pattern kind prefix and the actual
    pattern."""
    if ':' in pattern:
        kind, pat = pattern.split(':', 1)
        if kind in ('re', 'glob', 'path', 'relglob', 'relpath', 'relre',
                    'listfile', 'listfile0', 'set', 'include', 'subinclude',
                    'rootfilesin'):
            return kind, pat
    return default, pattern

def _globre(pat):
    r'''Convert an extended glob string to a regexp string.

    >>> print _globre(r'?')
    .
    >>> print _globre(r'*')
    [^/]*
    >>> print _globre(r'**')
    .*
    >>> print _globre(r'**/a')
    (?:.*/)?a
    >>> print _globre(r'a/**/b')
    a\/(?:.*/)?b
    >>> print _globre(r'[a*?!^][^b][!c]')
    [a*?!^][\^b][^c]
    >>> print _globre(r'{a,b}')
    (?:a|b)
    >>> print _globre(r'.\*\?')
    \.\*\?
    '''
    i, n = 0, len(pat)
    res = ''
    group = 0
    escape = util.re.escape
    def peek():
        return i < n and pat[i:i + 1]
    while i < n:
        c = pat[i:i + 1]
        i += 1
        if c not in '*?[{},\\':
            res += escape(c)
        elif c == '*':
            if peek() == '*':
                i += 1
                if peek() == '/':
                    i += 1
                    res += '(?:.*/)?'
                else:
                    res += '.*'
            else:
                res += '[^/]*'
        elif c == '?':
            res += '.'
        elif c == '[':
            j = i
            if j < n and pat[j:j + 1] in '!]':
                j += 1
            while j < n and pat[j:j + 1] != ']':
                j += 1
            if j >= n:
                res += '\\['
            else:
                stuff = pat[i:j].replace('\\','\\\\')
                i = j + 1
                if stuff[0:1] == '!':
                    stuff = '^' + stuff[1:]
                elif stuff[0:1] == '^':
                    stuff = '\\' + stuff
                res = '%s[%s]' % (res, stuff)
        elif c == '{':
            group += 1
            res += '(?:'
        elif c == '}' and group:
            res += ')'
            group -= 1
        elif c == ',' and group:
            res += '|'
        elif c == '\\':
            p = peek()
            if p:
                i += 1
                res += escape(p)
            else:
                res += escape(c)
        else:
            res += escape(c)
    return res

def _regex(kind, pat, globsuffix):
    '''Convert a (normalized) pattern of any kind into a regular expression.
    globsuffix is appended to the regexp of globs.'''
    if not pat:
        return ''
    if kind == 're':
        return pat
    if kind == 'path':
        if pat == '.':
            return ''
        return '^' + util.re.escape(pat) + '(?:/|$)'
    if kind == 'rootfilesin':
        if pat == '.':
            escaped = ''
        else:
            # Pattern is a directory name.
            escaped = util.re.escape(pat) + '/'
        # Anything after the pattern must be a non-directory.
        return '^' + escaped + '[^/]+$'
    if kind == 'relglob':
        return '(?:|.*/)' + _globre(pat) + globsuffix
    if kind == 'relpath':
        return util.re.escape(pat) + '(?:/|$)'
    if kind == 'relre':
        if pat.startswith('^'):
            return pat
        return '.*' + pat
    return _globre(pat) + globsuffix

def _buildmatch(ctx, kindpats, globsuffix, listsubrepos, root):
    '''Return regexp string and a matcher function for kindpats.
    globsuffix is appended to the regexp of globs.'''
    matchfuncs = []

    subincludes, kindpats = _expandsubinclude(kindpats, root)
    if subincludes:
        submatchers = {}
        def matchsubinclude(f):
            for prefix, matcherargs in subincludes:
                if f.startswith(prefix):
                    mf = submatchers.get(prefix)
                    if mf is None:
                        mf = match(*matcherargs)
                        submatchers[prefix] = mf

                    if mf(f[len(prefix):]):
                        return True
            return False
        matchfuncs.append(matchsubinclude)

    fset, kindpats = _expandsets(kindpats, ctx, listsubrepos)
    if fset:
        matchfuncs.append(fset.__contains__)

    regex = ''
    if kindpats:
        regex, mf = _buildregexmatch(kindpats, globsuffix)
        matchfuncs.append(mf)

    if len(matchfuncs) == 1:
        return regex, matchfuncs[0]
    else:
        return regex, lambda f: any(mf(f) for mf in matchfuncs)

def _buildregexmatch(kindpats, globsuffix):
    """Build a match function from a list of kinds and kindpats,
    return regexp string and a matcher function."""
    try:
        regex = '(?:%s)' % '|'.join([_regex(k, p, globsuffix)
                                     for (k, p, s) in kindpats])
        if len(regex) > 20000:
            raise OverflowError
        return regex, _rematcher(regex)
    except OverflowError:
        # We're using a Python with a tiny regex engine and we
        # made it explode, so we'll divide the pattern list in two
        # until it works
        l = len(kindpats)
        if l < 2:
            raise
        regexa, a = _buildregexmatch(kindpats[:l//2], globsuffix)
        regexb, b = _buildregexmatch(kindpats[l//2:], globsuffix)
        return regex, lambda s: a(s) or b(s)
    except re.error:
        for k, p, s in kindpats:
            try:
                _rematcher('(?:%s)' % _regex(k, p, globsuffix))
            except re.error:
                if s:
                    raise error.Abort(_("%s: invalid pattern (%s): %s") %
                                     (s, k, p))
                else:
                    raise error.Abort(_("invalid pattern (%s): %s") % (k, p))
        raise error.Abort(_("invalid pattern"))

def _patternrootsanddirs(kindpats):
    '''Returns roots and directories corresponding to each pattern.

    This calculates the roots and directories exactly matching the patterns and
    returns a tuple of (roots, dirs) for each. It does not return other
    directories which may also need to be considered, like the parent
    directories.
    '''
    r = []
    d = []
    for kind, pat, source in kindpats:
        if kind == 'glob': # find the non-glob prefix
            root = []
            for p in pat.split('/'):
                if '[' in p or '{' in p or '*' in p or '?' in p:
                    break
                root.append(p)
            r.append('/'.join(root) or '.')
        elif kind in ('relpath', 'path'):
            r.append(pat or '.')
        elif kind in ('rootfilesin',):
            d.append(pat or '.')
        else: # relglob, re, relre
            r.append('.')
    return r, d

def _roots(kindpats):
    '''Returns root directories to match recursively from the given patterns.'''
    roots, dirs = _patternrootsanddirs(kindpats)
    return roots

def _rootsanddirs(kindpats):
    '''Returns roots and exact directories from patterns.

    roots are directories to match recursively, whereas exact directories should
    be matched non-recursively. The returned (roots, dirs) tuple will also
    include directories that need to be implicitly considered as either, such as
    parent directories.

    >>> _rootsanddirs(\
        [('glob', 'g/h/*', ''), ('glob', 'g/h', ''), ('glob', 'g*', '')])
    (['g/h', 'g/h', '.'], ['g', '.'])
    >>> _rootsanddirs(\
        [('rootfilesin', 'g/h', ''), ('rootfilesin', '', '')])
    ([], ['g/h', '.', 'g', '.'])
    >>> _rootsanddirs(\
        [('relpath', 'r', ''), ('path', 'p/p', ''), ('path', '', '')])
    (['r', 'p/p', '.'], ['p', '.'])
    >>> _rootsanddirs(\
        [('relglob', 'rg*', ''), ('re', 're/', ''), ('relre', 'rr', '')])
    (['.', '.', '.'], ['.'])
    '''
    r, d = _patternrootsanddirs(kindpats)

    # Append the parents as non-recursive/exact directories, since they must be
    # scanned to get to either the roots or the other exact directories.
    d.extend(util.dirs(d))
    d.extend(util.dirs(r))
    # util.dirs() does not include the root directory, so add it manually
    d.append('.')

    return r, d

def _explicitfiles(kindpats):
    '''Returns the potential explicit filenames from the patterns.

    >>> _explicitfiles([('path', 'foo/bar', '')])
    ['foo/bar']
    >>> _explicitfiles([('rootfilesin', 'foo/bar', '')])
    []
    '''
    # Keep only the pattern kinds where one can specify filenames (vs only
    # directory names).
    filable = [kp for kp in kindpats if kp[0] not in ('rootfilesin',)]
    return _roots(filable)

def _anypats(kindpats):
    for kind, pat, source in kindpats:
        if kind in ('glob', 're', 'relglob', 'relre', 'set', 'rootfilesin'):
            return True

_commentre = None

def readpatternfile(filepath, warn, sourceinfo=False):
    '''parse a pattern file, returning a list of
    patterns. These patterns should be given to compile()
    to be validated and converted into a match function.

    trailing white space is dropped.
    the escape character is backslash.
    comments start with #.
    empty lines are skipped.

    lines can be of the following formats:

    syntax: regexp # defaults following lines to non-rooted regexps
    syntax: glob   # defaults following lines to non-rooted globs
    re:pattern     # non-rooted regular expression
    glob:pattern   # non-rooted glob
    pattern        # pattern of the current default type

    if sourceinfo is set, returns a list of tuples:
    (pattern, lineno, originalline). This is useful to debug ignore patterns.
    '''

    syntaxes = {'re': 'relre:', 'regexp': 'relre:', 'glob': 'relglob:',
                'include': 'include', 'subinclude': 'subinclude'}
    syntax = 'relre:'
    patterns = []

    fp = open(filepath, 'rb')
    for lineno, line in enumerate(util.iterfile(fp), start=1):
        if "#" in line:
            global _commentre
            if not _commentre:
                _commentre = util.re.compile(br'((?:^|[^\\])(?:\\\\)*)#.*')
            # remove comments prefixed by an even number of escapes
            m = _commentre.search(line)
            if m:
                line = line[:m.end(1)]
            # fixup properly escaped comments that survived the above
            line = line.replace("\\#", "#")
        line = line.rstrip()
        if not line:
            continue

        if line.startswith('syntax:'):
            s = line[7:].strip()
            try:
                syntax = syntaxes[s]
            except KeyError:
                if warn:
                    warn(_("%s: ignoring invalid syntax '%s'\n") %
                         (filepath, s))
            continue

        linesyntax = syntax
        for s, rels in syntaxes.iteritems():
            if line.startswith(rels):
                linesyntax = rels
                line = line[len(rels):]
                break
            elif line.startswith(s+':'):
                linesyntax = rels
                line = line[len(s) + 1:]
                break
        if sourceinfo:
            patterns.append((linesyntax + line, lineno, line))
        else:
            patterns.append(linesyntax + line)
    fp.close()
    return patterns
