# package.py - apt package abstraction
#
#  Copyright (c) 2005-2009 Canonical
#
#  Author: Michael Vogt <michael.vogt@ubuntu.com>
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License as
#  published by the Free Software Foundation; either version 2 of the
#  License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307
#  USA
"""Functionality related to packages."""
from __future__ import print_function


import os
import sys
import re
import socket
import subprocess

try:
    from http.client import BadStatusLine
    from urllib.error import HTTPError
    from urllib.request import urlopen
except ImportError:
    from httplib import BadStatusLine
    from urllib2 import HTTPError, urlopen

from collections import Mapping, Sequence

import apt_pkg
import apt.progress.text
from apt_pkg import gettext as _

__all__ = ('BaseDependency', 'Dependency', 'Origin', 'Package', 'Record',
           'Version', 'VersionList')

if sys.version_info.major >= 3:
    unicode = str


def _file_is_same(path, size, md5):
    """Return ``True`` if the file is the same."""
    if os.path.exists(path) and os.path.getsize(path) == size:
        with open(path) as fobj:
            return apt_pkg.md5sum(fobj) == md5


class FetchError(Exception):
    """Raised when a file could not be fetched."""


class BaseDependency(object):
    """A single dependency."""

    class __dstr(str):
        """Compare helper for compatibility with old third-party code.

        Old third-party code might still compare the relation with the
        previously used relations (<<,<=,==,!=,>=,>>,) instead of the curently
        used ones (<,<=,=,!=,>=,>,). This compare helper lets < match to <<,
        > match to >> and = match to ==.
        """

        def __eq__(self, other):
            if str.__eq__(self, other):
                return True
            elif str.__eq__(self, '<'):
                return str.__eq__('<<', other)
            elif str.__eq__(self, '>'):
                return str.__eq__('>>', other)
            elif str.__eq__(self, '='):
                return str.__eq__('==', other)
            else:
                return False

        def __ne__(self, other):
            return not self.__eq__(other)

    def __init__(self, dep):
        self._dep = dep  # apt_pkg.Dependency

    @property
    def name(self):
        """The name of the target package."""
        return self._dep.target_pkg.name

    @property
    def relation(self):
        """The relation (<, <=, !=, =, >=, >, ).

        Note that the empty string is a valid string as well, if no version
        is specified.
        """
        return self.__dstr(self._dep.comp_type)

    @property
    def version(self):
        """The target version or None.

        It is None if and only if relation is the empty string."""
        return self._dep.target_ver

    @property
    def rawtype(self):
        """Type of the dependency.

        This should be one of 'Breaks', 'Conflicts', 'Depends', 'Enhances',
        'PreDepends', 'Recommends', 'Replaces', 'Suggests'.

        Additional types might be added in the future.
        """
        return self._dep.dep_type_untranslated

    @property
    def pre_depend(self):
        """Whether this is a PreDepends."""
        return self._dep.dep_type_untranslated == 'PreDepends'

    def __repr__(self):
        return ('<BaseDependency: name:%r relation:%r version:%r preDepend:%r>'
                % (self.name, self.relation, self.version, self.pre_depend))


class Dependency(list):
    """Represent an Or-group of dependencies.

    Attributes defined here:
        or_dependencies - The possible choices
    """

    def __init__(self, alternatives):
        super(Dependency, self).__init__()
        self.extend(alternatives)

    @property
    def or_dependencies(self):
        return self


class Origin(object):
    """The origin of a version.

    Attributes defined here:
        archive   - The archive (eg. unstable)
        component - The component (eg. main)
        label     - The Label, as set in the Release file
        origin    - The Origin, as set in the Release file
        codename  - The Codename, as set in the Release file
        site      - The hostname of the site.
        trusted   - Boolean value whether this is trustworthy.
    """

    def __init__(self, pkg, packagefile):
        self.archive = packagefile.archive
        self.component = packagefile.component
        self.label = packagefile.label
        self.origin = packagefile.origin
        self.codename = packagefile.codename
        self.site = packagefile.site
        self.not_automatic = packagefile.not_automatic
        # check the trust
        indexfile = pkg._pcache._list.find_index(packagefile)
        if indexfile and indexfile.is_trusted:
            self.trusted = True
        else:
            self.trusted = False

    def __repr__(self):
        return ("<Origin component:%r archive:%r origin:%r label:%r "
                "site:%r isTrusted:%r>") % (self.component, self.archive,
                                            self.origin, self.label,
                                            self.site, self.trusted)


class Record(Mapping):
    """Record in a Packages file

    Represent a record as stored in a Packages file. You can use this like
    a dictionary mapping the field names of the record to their values::

        >>> record = Record("Package: python-apt\\nVersion: 0.8.0\\n\\n")
        >>> record["Package"]
        'python-apt'
        >>> record["Version"]
        '0.8.0'

    For example, to get the tasks of a package from a cache, you could do::

        package.candidate.record["Tasks"].split()

    Of course, you can also use the :attr:`Version.tasks` property.

    """

    def __init__(self, record_str):
        self._rec = apt_pkg.TagSection(record_str)

    def __hash__(self):
        return hash(self._rec)

    def __str__(self):
        return str(self._rec)

    def __getitem__(self, key):
        return self._rec[key]

    def __contains__(self, key):
        return key in self._rec

    def __iter__(self):
        return iter(self._rec.keys())

    def iteritems(self):
        """An iterator over the (key, value) items of the record."""
        for key in self._rec.keys():
            yield key, self._rec[key]

    def get(self, key, default=None):
        """Return record[key] if key in record, else *default*.

        The parameter *default* must be either a string or None.
        """
        return self._rec.get(key, default)

    def has_key(self, key):
        """deprecated form of ``key in x``."""
        return key in self._rec

    def __len__(self):
        return len(self._rec)


class Version(object):
    """Representation of a package version.

    The Version class contains all information related to a
    specific package version.

    .. versionadded:: 0.7.9
    """

    def __init__(self, package, cand):
        self.package = package
        self._cand = cand

    def _cmp(self, other):
        try:
            return apt_pkg.version_compare(self._cand.ver_str, other.version)
        except AttributeError:
            return apt_pkg.version_compare(self._cand.ver_str, other)

    def __eq__(self, other):
        try:
            return self._cmp(other) == 0
        except TypeError:
            return NotImplemented

    def __ge__(self, other):
        try:
            return self._cmp(other) >= 0
        except TypeError:
            return NotImplemented

    def __gt__(self, other):
        try:
            return self._cmp(other) > 0
        except TypeError:
            return NotImplemented

    def __le__(self, other):
        try:
            return self._cmp(other) <= 0
        except TypeError:
            return NotImplemented

    def __lt__(self, other):
        try:
            return self._cmp(other) < 0
        except TypeError:
            return NotImplemented

    def __ne__(self, other):
        try:
            return self._cmp(other) != 0
        except TypeError:
            return NotImplemented

    def __hash__(self):
        return self._cand.hash

    def __repr__(self):
        return '<Version: package:%r version:%r>' % (self.package.name,
                                                     self.version)

    @property
    def _records(self):
        """Internal helper that moves the Records to the right position."""
        if self.package._pcache._records.lookup(self._cand.file_list[0]):
            return self.package._pcache._records

    @property
    def _translated_records(self):
        """Internal helper to get the translated description."""
        desc_iter = self._cand.translated_description
        self.package._pcache._records.lookup(desc_iter.file_list.pop(0))
        return self.package._pcache._records

    @property
    def installed_size(self):
        """Return the size of the package when installed."""
        return self._cand.installed_size

    @property
    def homepage(self):
        """Return the homepage for the package."""
        return self._records.homepage

    @property
    def size(self):
        """Return the size of the package."""
        return self._cand.size

    @property
    def architecture(self):
        """Return the architecture of the package version."""
        return self._cand.arch

    @property
    def downloadable(self):
        """Return whether the version of the package is downloadable."""
        return bool(self._cand.downloadable)

    @property
    def version(self):
        """Return the version as a string."""
        return self._cand.ver_str

    @property
    def summary(self):
        """Return the short description (one line summary)."""
        return self._translated_records.short_desc

    @property
    def raw_description(self):
        """return the long description (raw)."""
        return self._records.long_desc

    @property
    def section(self):
        """Return the section of the package."""
        return self._cand.section

    @property
    def description(self):
        """Return the formatted long description.

        Return the formatted long description according to the Debian policy
        (Chapter 5.6.13).
        See http://www.debian.org/doc/debian-policy/ch-controlfields.html
        for more information.
        """
        desc = ''
        dsc = self._translated_records.long_desc
        try:
            if not isinstance(dsc, unicode):
                # Only convert where needed (i.e. Python 2.X)
                dsc = dsc.decode("utf-8")
        except UnicodeDecodeError as err:
            return _("Invalid unicode in description for '%s' (%s). "
                     "Please report.") % (self.package.name, err)

        lines = iter(dsc.split("\n"))
        # Skip the first line, since its a duplication of the summary
        next(lines)
        for raw_line in lines:
            if raw_line.strip() == ".":
                # The line is just line break
                if not desc.endswith("\n"):
                    desc += "\n\n"
                continue
            if raw_line.startswith("  "):
                # The line should be displayed verbatim without word wrapping
                if not desc.endswith("\n"):
                    line = "\n%s\n" % raw_line[2:]
                else:
                    line = "%s\n" % raw_line[2:]
            elif raw_line.startswith(" "):
                # The line is part of a paragraph.
                if desc.endswith("\n") or desc == "":
                    # Skip the leading white space
                    line = raw_line[1:]
                else:
                    line = raw_line
            else:
                line = raw_line
            # Add current line to the description
            desc += line
        return desc

    @property
    def source_name(self):
        """Return the name of the source package."""
        try:
            return self._records.source_pkg or self.package.shortname
        except IndexError:
            return self.package.shortname

    @property
    def source_version(self):
        """Return the version of the source package."""
        try:
            return self._records.source_ver or self._cand.ver_str
        except IndexError:
            return self._cand.ver_str

    @property
    def priority(self):
        """Return the priority of the package, as string."""
        return self._cand.priority_str

    @property
    def policy_priority(self):
        """Return the internal policy priority as a number.
           See apt_preferences(5) for more information about what it means.
        """
        priority = 0
        policy = self.package._pcache._depcache.policy
        for (packagefile, _) in self._cand.file_list:
            priority = max(priority, policy.get_priority(packagefile))
        return priority

    @property
    def record(self):
        """Return a Record() object for this version.

        Return a Record() object for this version which provides access
        to the raw attributes of the candidate version
        """
        return Record(self._records.record)

    def get_dependencies(self, *types):
        """Return a list of Dependency objects for the given types."""
        depends_list = []
        depends = self._cand.depends_list
        for type_ in types:
            try:
                for dep_ver_list in depends[type_]:
                    base_deps = []
                    for dep_or in dep_ver_list:
                        base_deps.append(BaseDependency(dep_or))
                    depends_list.append(Dependency(base_deps))
            except KeyError:
                pass
        return depends_list

    @property
    def provides(self):
        """ Return a list of names that this version provides."""
        return [p[0] for p in self._cand.provides_list]

    @property
    def enhances(self):
        """Return the list of enhances for the package version."""
        return self.get_dependencies("Enhances")

    @property
    def dependencies(self):
        """Return the dependencies of the package version."""
        return self.get_dependencies("PreDepends", "Depends")

    @property
    def recommends(self):
        """Return the recommends of the package version."""
        return self.get_dependencies("Recommends")

    @property
    def suggests(self):
        """Return the suggests of the package version."""
        return self.get_dependencies("Suggests")

    @property
    def origins(self):
        """Return a list of origins for the package version."""
        origins = []
        for (packagefile, _) in self._cand.file_list:
            origins.append(Origin(self.package, packagefile))
        return origins

    @property
    def filename(self):
        """Return the path to the file inside the archive.

        .. versionadded:: 0.7.10
        """
        return self._records.filename

    @property
    def md5(self):
        """Return the md5sum of the binary.

        .. versionadded:: 0.7.10
        """
        return self._records.md5_hash

    @property
    def sha1(self):
        """Return the sha1sum of the binary.

        .. versionadded:: 0.7.10
        """
        return self._records.sha1_hash

    @property
    def sha256(self):
        """Return the sha256sum of the binary.

        .. versionadded:: 0.7.10
        """
        return self._records.sha256_hash

    @property
    def tasks(self):
        """Get the tasks of the package.

        A set of the names of the tasks this package belongs to.

        .. versionadded:: 0.8.0
        """
        return set(self.record["Task"].split())

    def _uris(self):
        """Return an iterator over all available urls.

        .. versionadded:: 0.7.10
        """
        for (packagefile, _) in self._cand.file_list:
            indexfile = self.package._pcache._list.find_index(packagefile)
            if indexfile:
                yield indexfile.archive_uri(self._records.filename)

    @property
    def uris(self):
        """Return a list of all available uris for the binary.

        .. versionadded:: 0.7.10
        """
        return list(self._uris())

    @property
    def uri(self):
        """Return a single URI for the binary.

        .. versionadded:: 0.7.10
        """
        try:
            return next(iter(self._uris()))
        except StopIteration:
            return None

    def fetch_binary(self, destdir='', progress=None):
        """Fetch the binary version of the package.

        The parameter *destdir* specifies the directory where the package will
        be fetched to.

        The parameter *progress* may refer to an apt_pkg.AcquireProgress()
        object. If not specified or None, apt.progress.text.AcquireProgress()
        is used.

        .. versionadded:: 0.7.10
        """
        base = os.path.basename(self._records.filename)
        destfile = os.path.join(destdir, base)
        if _file_is_same(destfile, self.size, self._records.md5_hash):
            print(('Ignoring already existing file: %s' % destfile))
            return os.path.abspath(destfile)
        acq = apt_pkg.Acquire(progress or apt.progress.text.AcquireProgress())
        acqfile = apt_pkg.AcquireFile(acq, self.uri, self._records.md5_hash,
                                      self.size, base, destfile=destfile)
        acq.run()

        if acqfile.status != acqfile.STAT_DONE:
            raise FetchError("The item %r could not be fetched: %s" %
                             (acqfile.destfile, acqfile.error_text))

        return os.path.abspath(destfile)

    def fetch_source(self, destdir="", progress=None, unpack=True):
        """Get the source code of a package.

        The parameter *destdir* specifies the directory where the source will
        be fetched to.

        The parameter *progress* may refer to an apt_pkg.AcquireProgress()
        object. If not specified or None, apt.progress.text.AcquireProgress()
        is used.

        The parameter *unpack* describes whether the source should be unpacked
        (``True``) or not (``False``). By default, it is unpacked.

        If *unpack* is ``True``, the path to the extracted directory is
        returned. Otherwise, the path to the .dsc file is returned.
        """
        src = apt_pkg.SourceRecords()
        acq = apt_pkg.Acquire(progress or apt.progress.text.AcquireProgress())

        dsc = None
        record = self._records
        source_name = record.source_pkg or self.package.shortname
        source_version = record.source_ver or self._cand.ver_str
        source_lookup = src.lookup(source_name)

        while source_lookup and source_version != src.version:
            source_lookup = src.lookup(source_name)
        if not source_lookup:
            raise ValueError("No source for %r" % self)
        files = list()
        for md5, size, path, type_ in src.files:
            base = os.path.basename(path)
            destfile = os.path.join(destdir, base)
            if type_ == 'dsc':
                dsc = destfile
            if _file_is_same(destfile, size, md5):
                print(('Ignoring already existing file: %s' % destfile))
                continue
            files.append(apt_pkg.AcquireFile(acq, src.index.archive_uri(path),
                         md5, size, base, destfile=destfile))
        acq.run()

        for item in acq.items:
            if item.status != item.STAT_DONE:
                raise FetchError("The item %r could not be fetched: %s" %
                                 (item.destfile, item.error_text))

        if unpack:
            outdir = src.package + '-' + apt_pkg.upstream_version(src.version)
            outdir = os.path.join(destdir, outdir)
            subprocess.check_call(["dpkg-source", "-x", dsc, outdir])
            return os.path.abspath(outdir)
        else:
            return os.path.abspath(dsc)


class VersionList(Sequence):
    """Provide a mapping & sequence interface to all versions of a package.

    This class can be used like a dictionary, where version strings are the
    keys. It can also be used as a sequence, where integers are the keys.

    You can also convert this to a dictionary or a list, using the usual way
    of dict(version_list) or list(version_list). This is useful if you need
    to access the version objects multiple times, because they do not have to
    be recreated this way.

    Examples ('package.versions' being a version list):
        '0.7.92' in package.versions # Check whether 0.7.92 is a valid version.
        package.versions[0] # Return first version or raise IndexError
        package.versions[0:2] # Return a new VersionList for objects 0-2
        package.versions['0.7.92'] # Return version 0.7.92 or raise KeyError
        package.versions.keys() # All keys, as strings.
        max(package.versions)
    """

    def __init__(self, package, slice_=None):
        self._package = package  # apt.package.Package()
        self._versions = package._pkg.version_list  # [apt_pkg.Version(), ...]
        if slice_:
            self._versions = self._versions[slice_]

    def __getitem__(self, item):
        if isinstance(item, slice):
            return self.__class__(self._package, item)
        try:
            # Sequence interface, item is an integer
            return Version(self._package, self._versions[item])
        except TypeError:
            # Dictionary interface item is a string.
            for ver in self._versions:
                if ver.ver_str == item:
                    return Version(self._package, ver)
        raise KeyError("Version: %r not found." % (item))

    def __repr__(self):
        return '<VersionList: %r>' % self.keys()

    def __iter__(self):
        """Return an iterator over all value objects."""
        return (Version(self._package, ver) for ver in self._versions)

    def __contains__(self, item):
        if isinstance(item, Version):  # Sequence interface
            item = item.version
        # Dictionary interface.
        for ver in self._versions:
            if ver.ver_str == item:
                return True
        return False

    def __eq__(self, other):
        return list(self) == list(other)

    def __len__(self):
        return len(self._versions)

    # Mapping interface

    def keys(self):
        """Return a list of all versions, as strings."""
        return [ver.ver_str for ver in self._versions]

    def get(self, key, default=None):
        """Return the key or the default."""
        try:
            return self[key]
        except LookupError:
            return default


class Package(object):
    """Representation of a package in a cache.

    This class provides methods and properties for working with a package. It
    lets you mark the package for installation, check if it is installed, and
    much more.
    """

    def __init__(self, pcache, pkgiter):
        """ Init the Package object """
        self._pkg = pkgiter
        self._pcache = pcache           # python cache in cache.py
        self._changelog = ""            # Cached changelog

    def __repr__(self):
        return '<Package: name:%r architecture=%r id:%r>' % (
            self._pkg.name, self._pkg.architecture, self._pkg.id)

    def __lt__(self, other):
        return self.name < other.name

    def candidate(self):
        """Return the candidate version of the package.

        This property is writeable to allow you to set the candidate version
        of the package. Just assign a Version() object, and it will be set as
        the candidate version.
        """
        cand = self._pcache._depcache.get_candidate_ver(self._pkg)
        if cand is not None:
            return Version(self, cand)

    def __set_candidate(self, version):
        """Set the candidate version of the package."""
        self._pcache.cache_pre_change()
        self._pcache._depcache.set_candidate_ver(self._pkg, version._cand)
        self._pcache.cache_post_change()

    candidate = property(candidate, __set_candidate)

    @property
    def installed(self):
        """Return the currently installed version of the package.

        .. versionadded:: 0.7.9
        """
        if self._pkg.current_ver is not None:
            return Version(self, self._pkg.current_ver)

    @property
    def name(self):
        """Return the name of the package, possibly including architecture.

        If the package is not part of the system's preferred architecture,
        return the same as :attr:`fullname`, otherwise return the same
        as :attr:`shortname`

        .. versionchanged:: 0.7.100.3
        As part of multi-arch, this field now may include architecture
        information.
        """
        return self._pkg.get_fullname(True)

    @property
    def fullname(self):
        """Return the name of the package, including architecture.

        .. versionadded:: 0.7.100.3"""
        return self._pkg.get_fullname(False)

    @property
    def shortname(self):
        """Return the name of the package, without architecture.

        .. versionadded:: 0.7.100.3"""
        return self._pkg.name

    @property
    def id(self):
        """Return a uniq ID for the package.

        This can be used eg. to store additional information about the pkg."""
        return self._pkg.id

    def __hash__(self):
        """Return the hash of the object.

        This returns the same value as ID, which is unique."""
        return self._pkg.id

    @property
    def essential(self):
        """Return True if the package is an essential part of the system."""
        return self._pkg.essential

    def architecture(self):
        """Return the Architecture of the package.

        .. versionchanged:: 0.7.100.3
            This is now the package's architecture in the multi-arch sense,
            previously it was the architecture of the candidate version
            and deprecated.
        """
        return self._pkg.architecture

    @property
    def section(self):
        """Return the section of the package."""
        return self._pkg.section

    # depcache states

    @property
    def marked_install(self):
        """Return ``True`` if the package is marked for install."""
        return self._pcache._depcache.marked_install(self._pkg)

    @property
    def marked_upgrade(self):
        """Return ``True`` if the package is marked for upgrade."""
        return self._pcache._depcache.marked_upgrade(self._pkg)

    @property
    def marked_delete(self):
        """Return ``True`` if the package is marked for delete."""
        return self._pcache._depcache.marked_delete(self._pkg)

    @property
    def marked_keep(self):
        """Return ``True`` if the package is marked for keep."""
        return self._pcache._depcache.marked_keep(self._pkg)

    @property
    def marked_downgrade(self):
        """ Package is marked for downgrade """
        return self._pcache._depcache.marked_downgrade(self._pkg)

    @property
    def marked_reinstall(self):
        """Return ``True`` if the package is marked for reinstall."""
        return self._pcache._depcache.marked_reinstall(self._pkg)

    @property
    def is_installed(self):
        """Return ``True`` if the package is installed."""
        return (self._pkg.current_ver is not None)

    @property
    def is_upgradable(self):
        """Return ``True`` if the package is upgradable."""
        return (self.is_installed and
                self._pcache._depcache.is_upgradable(self._pkg))

    @property
    def is_auto_removable(self):
        """Return ``True`` if the package is no longer required.

        If the package has been installed automatically as a dependency of
        another package, and if no packages depend on it anymore, the package
        is no longer required.
        """
        return ((self.is_installed or self.marked_install) and
                self._pcache._depcache.is_garbage(self._pkg))

    @property
    def is_auto_installed(self):
        """Return whether the package is marked as automatically installed."""
        return self._pcache._depcache.is_auto_installed(self._pkg)
    # sizes

    @property
    def installed_files(self):
        """Return a list of files installed by the package.

        Return a list of unicode names of the files which have
        been installed by this package
        """
        for name in self.shortname, self.fullname:
            path = "/var/lib/dpkg/info/%s.list" % name
            try:
                with open(path, "rb") as file_list:
                    return file_list.read().decode("utf-8").split(u"\n")
            except EnvironmentError:
                continue

        return []

    def get_changelog(self, uri=None, cancel_lock=None):
        """
        Download the changelog of the package and return it as unicode
        string.

        The parameter *uri* refers to the uri of the changelog file. It may
        contain multiple named variables which will be substitued. These
        variables are (src_section, prefix, src_pkg, src_ver). An example is
        the Ubuntu changelog::

            "http://changelogs.ubuntu.com/changelogs/pool" \\
                "/%(src_section)s/%(prefix)s/%(src_pkg)s" \\
                "/%(src_pkg)s_%(src_ver)s/changelog"

        The parameter *cancel_lock* refers to an instance of threading.Lock,
        which if set, prevents the download.
        """
        # Return a cached changelog if available
        if self._changelog != u"":
            return self._changelog

        if uri is None:
            if not self.candidate:
                pass
            if self.candidate.origins[0].origin == "Debian":
                uri = "http://packages.debian.org/changelogs/pool" \
                      "/%(src_section)s/%(prefix)s/%(src_pkg)s" \
                      "/%(src_pkg)s_%(src_ver)s/changelog"
            elif self.candidate.origins[0].origin == "Ubuntu":
                uri = "http://changelogs.ubuntu.com/changelogs/pool" \
                      "/%(src_section)s/%(prefix)s/%(src_pkg)s" \
                      "/%(src_pkg)s_%(src_ver)s/changelog"
            else:
                res = _("The list of changes is not available")
                return res if isinstance(res, unicode) else res.decode("utf-8")

        # get the src package name
        src_pkg = self.candidate.source_name

        # assume "main" section
        src_section = "main"
        # use the section of the candidate as a starting point
        section = self.candidate.section

        # get the source version
        src_ver = self.candidate.source_version

        try:
            # try to get the source version of the pkg, this differs
            # for some (e.g. libnspr4 on ubuntu)
            # this feature only works if the correct deb-src are in the
            # sources.list otherwise we fall back to the binary version number
            src_records = apt_pkg.SourceRecords()
        except SystemError:
            pass
        else:
            while src_records.lookup(src_pkg):
                if not src_records.version:
                    continue
                if self.candidate.source_version == src_records.version:
                    # Direct match, use it and do not do more lookups.
                    src_ver = src_records.version
                    section = src_records.section
                    break
                if apt_pkg.version_compare(src_records.version, src_ver) > 0:
                    # The version is higher, it seems to match.
                    src_ver = src_records.version
                    section = src_records.section

        section_split = section.split("/", 1)
        if len(section_split) > 1:
            src_section = section_split[0]
        del section_split

        # lib is handled special
        prefix = src_pkg[0]
        if src_pkg.startswith("lib"):
            prefix = "lib" + src_pkg[3]

        # stip epoch
        src_ver_split = src_ver.split(":", 1)
        if len(src_ver_split) > 1:
            src_ver = "".join(src_ver_split[1:])
        del src_ver_split

        uri = uri % {"src_section": src_section,
                     "prefix": prefix,
                     "src_pkg": src_pkg,
                     "src_ver": src_ver}

        timeout = socket.getdefaulttimeout()

        # FIXME: when python2.4 vanishes from the archive,
        #        merge this into a single try..finally block (pep 341)
        try:
            try:
                # Set a timeout for the changelog download
                socket.setdefaulttimeout(2)

                # Check if the download was canceled
                if cancel_lock and cancel_lock.isSet():
                    return u""
                # FIXME: python3.2: Should be closed manually
                changelog_file = urlopen(uri)
                # do only get the lines that are new
                changelog = u""
                regexp = "^%s \((.*)\)(.*)$" % (re.escape(src_pkg))
                while True:
                    # Check if the download was canceled
                    if cancel_lock and cancel_lock.isSet():
                        return u""
                    # Read changelog line by line
                    line_raw = changelog_file.readline()
                    if not line_raw:
                        break
                    # The changelog is encoded in utf-8, but since there isn't
                    # any http header, urllib2 seems to treat it as ascii
                    line = line_raw.decode("utf-8")

                    #print line.encode('utf-8')
                    match = re.match(regexp, line)
                    if match:
                        # strip epoch from installed version
                        # and from changelog too
                        installed = getattr(self.installed, 'version', None)
                        if installed and ":" in installed:
                            installed = installed.split(":", 1)[1]
                        changelog_ver = match.group(1)
                        if changelog_ver and ":" in changelog_ver:
                            changelog_ver = changelog_ver.split(":", 1)[1]

                        if (installed and apt_pkg.version_compare(
                                changelog_ver, installed) <= 0):
                            break
                    # EOF (shouldn't really happen)
                    changelog += line

                # Print an error if we failed to extract a changelog
                if len(changelog) == 0:
                    changelog = _("The list of changes is not available")
                    if not isinstance(changelog, unicode):
                        changelog = changelog.decode("utf-8")
                self._changelog = changelog

            except HTTPError:
                res = _("The list of changes is not available yet.\n\n"
                        "Please use http://launchpad.net/ubuntu/+source/%s/"
                        "%s/+changelog\n"
                        "until the changes become available or try again "
                        "later.") % (src_pkg, src_ver)
                return res if isinstance(res, unicode) else res.decode("utf-8")
            except (IOError, BadStatusLine):
                res = _("Failed to download the list of changes. \nPlease "
                        "check your Internet connection.")
                return res if isinstance(res, unicode) else res.decode("utf-8")
        finally:
            socket.setdefaulttimeout(timeout)
        return self._changelog

    @property
    def versions(self):
        """Return a VersionList() object for all available versions.

        .. versionadded:: 0.7.9
        """
        return VersionList(self)

    @property
    def is_inst_broken(self):
        """Return True if the to-be-installed package is broken."""
        return self._pcache._depcache.is_inst_broken(self._pkg)

    @property
    def is_now_broken(self):
        """Return True if the installed package is broken."""
        return self._pcache._depcache.is_now_broken(self._pkg)

    @property
    def has_config_files(self):
        """Checks whether the package is is the config-files state."""
        return self. _pkg.current_state == apt_pkg.CURSTATE_CONFIG_FILES

    # depcache actions

    def mark_keep(self):
        """Mark a package for keep."""
        self._pcache.cache_pre_change()
        self._pcache._depcache.mark_keep(self._pkg)
        self._pcache.cache_post_change()

    def mark_delete(self, auto_fix=True, purge=False):
        """Mark a package for deletion.

        If *auto_fix* is ``True``, the resolver will be run, trying to fix
        broken packages.  This is the default.

        If *purge* is ``True``, remove the configuration files of the package
        as well.  The default is to keep the configuration.
        """
        self._pcache.cache_pre_change()
        self._pcache._depcache.mark_delete(self._pkg, purge)
        # try to fix broken stuffsta
        if auto_fix and self._pcache._depcache.broken_count > 0:
            fix = apt_pkg.ProblemResolver(self._pcache._depcache)
            fix.clear(self._pkg)
            fix.protect(self._pkg)
            fix.remove(self._pkg)
            fix.install_protect()
            fix.resolve()
        self._pcache.cache_post_change()

    def mark_install(self, auto_fix=True, auto_inst=True, from_user=True):
        """Mark a package for install.

        If *autoFix* is ``True``, the resolver will be run, trying to fix
        broken packages.  This is the default.

        If *autoInst* is ``True``, the dependencies of the packages will be
        installed automatically.  This is the default.

        If *fromUser* is ``True``, this package will not be marked as
        automatically installed. This is the default. Set it to False if you
        want to be able to automatically remove the package at a later stage
        when no other package depends on it.
        """
        self._pcache.cache_pre_change()
        self._pcache._depcache.mark_install(self._pkg, auto_inst, from_user)
        # try to fix broken stuff
        if auto_fix and self._pcache._depcache.broken_count > 0:
            fixer = apt_pkg.ProblemResolver(self._pcache._depcache)
            fixer.clear(self._pkg)
            fixer.protect(self._pkg)
            fixer.resolve(True)
        self._pcache.cache_post_change()

    def mark_upgrade(self, from_user=True):
        """Mark a package for upgrade."""
        if self.is_upgradable:
            auto = self.is_auto_installed
            self.mark_install(from_user=from_user)
            self.mark_auto(auto)
        else:
            # FIXME: we may want to throw a exception here
            sys.stderr.write(("MarkUpgrade() called on a non-upgrable pkg: "
                              "'%s'\n") % self._pkg.name)

    def mark_auto(self, auto=True):
        """Mark a package as automatically installed.

        Call this function to mark a package as automatically installed. If the
        optional parameter *auto* is set to ``False``, the package will not be
        marked as automatically installed anymore. The default is ``True``.
        """
        self._pcache._depcache.mark_auto(self._pkg, auto)

    def commit(self, fprogress, iprogress):
        """Commit the changes.

        The parameter *fprogress* refers to a apt_pkg.AcquireProgress() object,
        like apt.progress.text.AcquireProgress().

        The parameter *iprogress* refers to an InstallProgress() object, as
        found in apt.progress.base.
        """
        self._pcache._depcache.commit(fprogress, iprogress)


def _test():
    """Self-test."""
    print("Self-test for the Package modul")
    import random
    apt_pkg.init()
    progress = apt.progress.text.OpProgress()
    cache = apt.Cache(progress)
    pkg = cache["apt-utils"]
    print("Name: %s " % pkg.name)
    print("ID: %s " % pkg.id)
    print("Priority (Candidate): %s " % pkg.candidate.priority)
    print("Priority (Installed): %s " % pkg.installed.priority)
    print("Installed: %s " % pkg.installed.version)
    print("Candidate: %s " % pkg.candidate.version)
    print("CandidateDownloadable: %s" % pkg.candidate.downloadable)
    print("CandidateOrigins: %s" % pkg.candidate.origins)
    print("SourcePkg: %s " % pkg.candidate.source_name)
    print("Section: %s " % pkg.section)
    print("Summary: %s" % pkg.candidate.summary)
    print("Description (formatted) :\n%s" % pkg.candidate.description)
    print("Description (unformatted):\n%s" % pkg.candidate.raw_description)
    print("InstalledSize: %s " % pkg.candidate.installed_size)
    print("PackageSize: %s " % pkg.candidate.size)
    print("Dependencies: %s" % pkg.installed.dependencies)
    print("Recommends: %s" % pkg.installed.recommends)
    for dep in pkg.candidate.dependencies:
        print(",".join("%s (%s) (%s) (%s)" % (o.name, o.version, o.relation,
                       o.pre_depend) for o in dep.or_dependencies))
    print("arch: %s" % pkg.candidate.architecture)
    print("homepage: %s" % pkg.candidate.homepage)
    print("rec: ", pkg.candidate.record)

    print(cache["2vcard"].get_changelog())
    for i in True, False:
        print("Running install on random upgradable pkgs with AutoFix: ", i)
        for pkg in cache:
            if pkg.is_upgradable:
                if random.randint(0, 1) == 1:
                    pkg.mark_install(i)
        print("Broken: %s " % cache._depcache.broken_count)
        print("InstCount: %s " % cache._depcache.inst_count)

    print()
    # get a new cache
    for i in True, False:
        print("Randomly remove some packages with AutoFix: %s" % i)
        cache = apt.Cache(progress)
        for name in cache.keys():
            if random.randint(0, 1) == 1:
                try:
                    cache[name].mark_delete(i)
                except SystemError:
                    print("Error trying to remove: %s " % name)
        print("Broken: %s " % cache._depcache.broken_count)
        print("DelCount: %s " % cache._depcache.del_count)

# self-test
if __name__ == "__main__":
    _test()
