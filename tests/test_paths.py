#
# Test that both unicode and bytes path names work
#
import os
import shutil
import unittest

import apt_inst
import apt_pkg


class TestPath(unittest.TestCase):

    dir_unicode = u'data/tmp'
    dir_bytes = b'data/tmp'
    file_unicode = u'data/tmp/python-apt-test'
    file_bytes = b'data/tmp/python-apt-test'

    def setUp(self):
        apt_pkg.init()
        if os.path.exists(self.dir_bytes):
            shutil.rmtree(self.dir_bytes)

        os.mkdir(self.dir_bytes)

    def tearDown(self):
        apt_pkg.config["dir"] = "/"
        shutil.rmtree(self.dir_bytes)

    def test_acquire(self):
        apt_pkg.AcquireFile(apt_pkg.Acquire(), "http://example.com",
                            destdir=self.file_bytes, destfile=self.file_bytes)
        apt_pkg.AcquireFile(apt_pkg.Acquire(),
                            "http://example.com",
                            destdir=self.file_unicode,
                            destfile=self.file_unicode)

    def test_ararchive(self):
        archive = apt_inst.ArArchive(u"data/test_debs/data-tar-xz.deb")

        apt_inst.ArArchive(b"data/test_debs/data-tar-xz.deb")

        archive.extract(u"debian-binary", u"data/tmp")
        archive.extract(b"debian-binary", b"data/tmp")
        archive.extractall(u"data/tmp")
        archive.extractall(b"data/tmp")
        self.assertEqual(archive.extractdata(u"debian-binary"), b"2.0\n")
        self.assertEqual(archive.extractdata(b"debian-binary"), b"2.0\n")
        self.assertTrue(archive.getmember(u"debian-binary"))
        self.assertTrue(archive.getmember(b"debian-binary"))
        self.assertTrue(u"debian-binary" in archive)
        self.assertTrue(b"debian-binary" in archive)
        self.assertTrue(archive[b"debian-binary"])
        self.assertTrue(archive[u"debian-binary"])

        tar = archive.gettar(u"control.tar.gz", "gzip")
        tar = archive.gettar(b"control.tar.gz", "gzip")

        tar.extractall(self.dir_unicode)
        tar.extractall(self.dir_bytes)
        self.assertRaises(LookupError, tar.extractdata, u"Do-not-exist")
        self.assertRaises(LookupError, tar.extractdata, b"Do-not-exist")
        tar.extractdata(b"control")
        tar.extractdata(u"control")

        apt_inst.TarFile(os.path.join(self.dir_unicode, u"control.tar.gz"))
        apt_inst.TarFile(os.path.join(self.dir_bytes, b"control.tar.gz"))

    def test_configuration(self):
        with open(self.file_unicode, 'w') as config:
            config.write("Hello { World 1; };")
        apt_pkg.read_config_file(apt_pkg.config, self.file_bytes)
        apt_pkg.read_config_file(apt_pkg.config, self.file_unicode)
        apt_pkg.read_config_file_isc(apt_pkg.config, self.file_bytes)
        apt_pkg.read_config_file_isc(apt_pkg.config, self.file_unicode)
        apt_pkg.read_config_dir(apt_pkg.config, self.dir_unicode)
        apt_pkg.read_config_dir(apt_pkg.config, b"/etc/apt/apt.conf.d")

    def test_index_file(self):
        apt_pkg.config["dir"] = "data/test_debs"
        slist = apt_pkg.SourceList()
        slist.read_main_list()

        for meta in slist.list:
            for index in meta.index_files:
                index.archive_uri(self.file_bytes)
                index.archive_uri(self.file_unicode)

    def test_index_records(self):
        index = apt_pkg.IndexRecords()
        index.load(u"./data/misc/foo_Release")
        index.load(b"./data/misc/foo_Release")

        hash1, size1 = index.lookup(u"main/i18n/Index")
        hash2, size2 = index.lookup(b"main/i18n/Index")

        self.assertEqual(size1, size2)
        self.assertEqual(str(hash1), str(hash2))
        self.assertEqual(str(hash1), ("SHA256:fefed230e286d832ab6eb0fb7b72"
                                      "442165b50df23a68402ae6e9d265a31920a2"))

    def test_lock(self):
        apt_pkg.get_lock(self.file_unicode, True)
        apt_pkg.get_lock(self.file_bytes, True)

        with apt_pkg.FileLock(self.file_unicode):
            pass
        with apt_pkg.FileLock(self.file_bytes):
            pass

    def test_policy(self):
        apt_pkg.config["dir"] = "data/test_debs"
        cache = apt_pkg.Cache(None)
        policy = apt_pkg.Policy(cache)
        file_unicode = os.path.join(self.dir_unicode, u"test.prefs")
        file_bytes = os.path.join(self.dir_bytes, b"test.prefs")

        self.assertTrue(policy.read_pinfile(file_unicode))
        self.assertTrue(policy.read_pinfile(file_bytes))
        self.assertTrue(policy.read_pindir(self.dir_unicode))
        self.assertTrue(policy.read_pindir(self.dir_bytes))

    def test_tag(self):
        with open(self.file_bytes, "w") as tag:
            tag.write("Key: value\n")
        tag1 = apt_pkg.TagFile(self.file_unicode)
        tag2 = apt_pkg.TagFile(self.file_bytes)

        self.assertEqual(next(tag1)["Key"], "value")
        self.assertEqual(next(tag2)["Key"], "value")

        self.assertRaises(StopIteration, next, tag1)
        self.assertRaises(StopIteration, next, tag2)

if __name__ == '__main__':
    unittest.main()
