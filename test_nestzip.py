"""Self-check: python -m unittest test_nestzip -v"""
import os
import random
import shutil
import tempfile
import unittest
import zipfile
from unittest import mock

import nestzip


def make_tree(root):
    """A small folder with random + compressible files, nesting and an empty dir."""
    rnd = random.Random(42)
    files = {}
    for i in range(12):
        rel = f"sub{i % 3}/deep/f{i}.bin" if i % 2 else f"f{i}.txt"
        data = rnd.randbytes(rnd.randint(0, 3000)) if i % 2 else b"hello nestzip " * rnd.randint(1, 300)
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        files[rel] = data
    os.makedirs(os.path.join(root, "empty", "dir"))
    return files


class NestzipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "data")
        self.arc = self.src + ".zip"
        os.makedirs(self.src)
        self.files = make_tree(self.src)
        self.args = ["pack", self.src, "--batch", "4KB", "--keep-free", "0", "-y"]

    def tearDown(self):
        self.tmp.cleanup()

    def assert_finished(self):
        self.assertFalse(os.path.exists(self.src), "source folder should be gone")
        self.assertFalse(os.path.exists(self.arc + nestzip.SIDE), "sidecar should be gone")
        with zipfile.ZipFile(self.arc) as z:
            self.assertIsNone(z.testzip())
            self.assertIn("empty/dir/", z.namelist())
            for rel, data in self.files.items():
                self.assertEqual(z.read(rel), data, rel)

    def test_pack(self):
        nestzip.main(self.args)
        self.assert_finished()

    def test_power_cut_mid_batch(self):
        """Batch 2 dies half-written over the central directory, with no cleanup at all."""
        real, calls = nestzip.commit, []

        def crashing(arc, batch, method):
            calls.append(1)
            if len(calls) == 2:
                with zipfile.ZipFile(arc) as z:
                    start = z.start_dir
                with open(arc, "r+b") as f:
                    f.seek(start)
                    f.write(b"PK\x03\x04 half a header then the power goes out")
                raise KeyboardInterrupt
            real(arc, batch, method)

        with mock.patch.object(nestzip, "commit", crashing), self.assertRaises(SystemExit) as cm:
            nestzip.main(self.args)
        self.assertEqual(cm.exception.code, 130)
        with self.assertRaises(zipfile.BadZipFile):
            zipfile.ZipFile(self.arc)  # really broken before resume
        nestzip.main(self.args)
        self.assert_finished()

    def test_killed_while_deleting(self):
        """Batch saved, killed after deleting only some originals."""
        real, calls = nestzip.remove, []

        def dying(path):
            calls.append(1)
            if len(calls) == 3:
                raise KeyboardInterrupt
            real(path)

        with mock.patch.object(nestzip, "remove", dying), self.assertRaises(SystemExit):
            nestzip.main(self.args)
        nestzip.main(self.args)
        self.assert_finished()

    def test_file_changed_since_packed_is_refused(self):
        with mock.patch.object(nestzip, "remove", side_effect=KeyboardInterrupt), self.assertRaises(SystemExit):
            nestzip.main(self.args)  # first file packed, nothing deleted
        with open(os.path.join(self.src, "f0.txt"), "ab") as f:
            f.write(b"edited")
        with self.assertRaises(SystemExit) as cm:
            nestzip.main(self.args)
        self.assertIn("different content", str(cm.exception.code))
        self.assertTrue(os.path.exists(os.path.join(self.src, "f0.txt")), "edited file must not be deleted")

    def test_not_enough_space_changes_nothing(self):
        with mock.patch("shutil.disk_usage", return_value=shutil._ntuple_diskusage(0, 0, 10)), self.assertRaises(SystemExit):
            nestzip.main(self.args)
        self.assertFalse(os.path.exists(self.arc))
        self.assertEqual(len(nestzip.scan(self.src)[0]), len(self.files) + 1)


if __name__ == "__main__":
    unittest.main()
