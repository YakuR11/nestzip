"""Self-check: python -m unittest -v"""
import io
import os
import random
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


def make_link(target, link):
    """A directory link: a junction on Windows (no admin rights needed), a symlink elsewhere."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(target, link)
    else:
        os.symlink(target, link, target_is_directory=True)


class NestzipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "data")
        self.arc = self.src + ".zip"
        os.makedirs(self.src)
        self.files = make_tree(self.src)
        self.args = ["pack", self.src, "--batch", "4KB", "--keep-free", "0", "-y"]
        for stream in ("sys.stdout", "sys.stderr"):
            quiet = mock.patch(stream, io.StringIO())
            quiet.start()
            self.addCleanup(quiet.stop)

    def tearDown(self):
        nestzip.STOP.clear()
        self.tmp.cleanup()

    def pack(self, *extra):
        nestzip.main(self.args + list(extra))

    def assert_exits(self, code_or_text, *args):
        with self.assertRaises(SystemExit) as cm:
            nestzip.main(list(args) if args else self.args)
        if isinstance(code_or_text, int):
            self.assertEqual(cm.exception.code, code_or_text)
        else:
            self.assertIn(code_or_text, str(cm.exception.code))

    def assert_finished(self):
        self.assertFalse(os.path.exists(self.src), "source folder should be gone")
        self.assertEqual(os.listdir(self.tmp.name), ["data.zip"], "only the archive should be left")
        with zipfile.ZipFile(self.arc) as z:
            self.assertIsNone(z.testzip())
            self.assertIn("empty/dir/", z.namelist())
            for rel, data in self.files.items():
                self.assertEqual(z.read(rel), data, rel)

    def in_progress(self):
        """Leave a run stopped after its first batch was saved, before any original was deleted."""
        with mock.patch.object(nestzip, "remove", side_effect=KeyboardInterrupt), self.assertRaises(SystemExit):
            self.pack()

    def half_write(self):
        """What a power cut in the middle of a batch leaves: junk over the central directory."""
        with zipfile.ZipFile(self.arc) as z:
            start = z.start_dir
        with open(self.arc, "r+b") as f:
            f.seek(start)
            f.write(b"PK\x03\x04 half a header then the power goes out")

    # The normal path

    def test_pack(self):
        self.pack()
        self.assert_finished()

    def test_pack_store(self):
        self.pack("--store")
        self.assert_finished()
        with zipfile.ZipFile(self.arc) as z:
            self.assertTrue(all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist()))

    def test_check_changes_nothing(self):
        self.assert_exits(0, "check", self.src, "--keep-free", "0")
        self.assertEqual(os.listdir(self.tmp.name), ["data"])

    def test_exclude_leaves_files_alone(self):
        self.pack("--exclude", "*.bin", "--exclude", "empty")
        with zipfile.ZipFile(self.arc) as z:
            names = z.namelist()
            for rel, data in self.files.items():
                if rel.endswith(".bin"):
                    self.assertNotIn(rel, names)
                    self.assertTrue(os.path.exists(os.path.join(self.src, *rel.split("/"))), rel)
                else:
                    self.assertEqual(z.read(rel), data, rel)
        self.assertNotIn("empty/dir/", names)
        self.assertTrue(os.path.isdir(os.path.join(self.src, "empty", "dir")))
        self.assertFalse(os.path.exists(self.arc + nestzip.SIDECAR))

    def test_double_click_asks_for_the_folder(self):
        """No arguments: the folder is dragged in (with quotes), confirmed, packed, then Enter closes."""
        answers = iter([f'"{self.src}"', "y", ""])
        with mock.patch("builtins.input", lambda prompt="": next(answers)):
            nestzip.interactive()
        self.assert_finished()
        self.assertIsNone(next(answers, None), "the window must wait for Enter before closing")

    def test_double_click_then_no_changes_nothing(self):
        answers = iter([self.src, "n", ""])
        with mock.patch("builtins.input", lambda prompt="": next(answers)):
            nestzip.interactive()
        self.assertEqual(os.listdir(self.tmp.name), ["data"])

    def test_stop_button_stops_between_batches(self):
        """The window's Stop button: the run ends after a whole batch, and resumes like any pause."""
        real = nestzip.commit

        def commit_then_press_stop(*args):
            real(*args)
            nestzip.STOP.set()

        with mock.patch.object(nestzip, "commit", commit_then_press_stop):
            self.assert_exits(nestzip.EXIT_PAUSED)
        with zipfile.ZipFile(self.arc) as z:
            self.assertIsNone(z.testzip(), "the archive must be valid when stopped")
        nestzip.STOP.clear()
        self.pack()
        self.assert_finished()

    @unittest.skipUnless(os.name == "nt", "the right-click menu is a Windows feature")
    def test_right_click_menu(self):
        key = r"Software\nestzip-test"  # directly under Software, so removing it leaves nothing behind
        self.addCleanup(nestzip.set_menu, False, key)
        nestzip.set_menu(True, key)
        self.assertTrue(nestzip.menu_installed(key))
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key + r"\command") as command:
            self.assertTrue(winreg.QueryValue(command, None).endswith(' gui "%1"'))
        nestzip.set_menu(False, key)
        self.assertFalse(nestzip.menu_installed(key))

    def test_progress_and_duration(self):
        self.assertEqual(nestzip.duration(12.44), "12.4 s")
        self.assertEqual(nestzip.duration(125), "2 min 05 s")
        self.assertEqual(nestzip.duration(3780), "1 h 03 min")
        self.assertEqual(nestzip.progress(0, 100, 5), "")
        self.assertEqual(nestzip.progress(100, 100, 5), "")
        self.assertEqual(nestzip.progress(25, 100, 10), ", 25% done, about 30.0 s left")

    def test_files_dated_before_1980(self):
        os.utime(os.path.join(self.src, "f0.txt"), (0, 0))  # 1970, common after a bad copy
        self.pack()
        self.assert_finished()

    # Interruptions

    def test_power_cut_mid_batch(self):
        """Batch 2 dies half-written over the central directory, with no cleanup at all."""
        real, calls = nestzip.commit, []

        def crashing(arc, batch, method, folder):
            calls.append(1)
            if len(calls) == 2:
                self.half_write()
                raise KeyboardInterrupt
            real(arc, batch, method, folder)

        with mock.patch.object(nestzip, "commit", crashing):
            self.assert_exits(nestzip.EXIT_PAUSED)
        with self.assertRaises(zipfile.BadZipFile):
            zipfile.ZipFile(self.arc)  # really broken before resume
        self.pack()
        self.assert_finished()

    def test_killed_while_deleting(self):
        """Batch saved, killed after deleting only some originals."""
        real, calls = nestzip.remove, []

        def dying(path, folder):
            calls.append(1)
            if len(calls) == 3:
                raise KeyboardInterrupt
            real(path, folder)

        with mock.patch.object(nestzip, "remove", dying), self.assertRaises(SystemExit):
            self.pack()
        self.pack()
        self.assert_finished()

    def test_killed_after_emptying_a_folder(self):
        """sub0/deep/ and empty/ end up empty: on resume they're in the archive through their files."""
        real, calls = nestzip.remove, []

        def dying(path, folder):
            calls.append(1)
            if len(calls) == 10:  # 6 root files, empty/dir/, then both files of sub0/deep/ are gone
                raise KeyboardInterrupt
            real(path, folder)

        with mock.patch.object(nestzip, "remove", dying), self.assertRaises(SystemExit):
            self.pack("--batch", "1GB")
        self.assertEqual(os.listdir(os.path.join(self.src, "sub0", "deep")), [])
        self.pack()
        self.assert_finished()
        with zipfile.ZipFile(self.arc) as z:
            self.assertNotIn("sub0/deep/", z.namelist(), "a resumed archive should match an uninterrupted one")

    def test_killed_before_first_batch(self):
        """An empty archive with no sidecar is what a run stopped right at the start leaves."""
        zipfile.ZipFile(self.arc, "w").close()
        self.pack()
        self.assert_finished()

    def test_folder_gone_with_broken_archive(self):
        """Folder moved away mid-run: the archive is repaired before the sidecar is dropped."""
        self.in_progress()
        self.half_write()
        os.rename(self.src, self.src + "-moved")
        self.pack()
        self.assertFalse(os.path.exists(self.arc + nestzip.SIDECAR))
        with zipfile.ZipFile(self.arc) as z:
            self.assertIsNone(z.testzip())

    # Refusals: each must leave everything as it was

    def test_file_changed_since_packed_is_refused(self):
        self.in_progress()
        with open(os.path.join(self.src, "f0.txt"), "ab") as f:
            f.write(b"edited")
        self.assert_exits("different content")
        self.assertTrue(os.path.exists(os.path.join(self.src, "f0.txt")), "edited file must not be deleted")

    def test_not_enough_space_changes_nothing(self):
        with mock.patch("shutil.disk_usage", return_value=mock.Mock(free=10)):
            self.assert_exits(nestzip.EXIT_NO_SPACE)
            self.assert_exits(nestzip.EXIT_NO_SPACE, "check", self.src)
        self.assertEqual(os.listdir(self.tmp.name), ["data"])
        self.assertEqual(len(nestzip.scan(self.src)[0]), len(self.files) + 1)

    def test_damaged_sidecar_never_truncates(self):
        self.in_progress()
        with open(self.arc, "rb") as f:
            before = f.read()
        with open(self.arc + nestzip.SIDECAR, "r+b") as f:
            f.seek(-1, os.SEEK_END)
            f.write(b"X")
        self.assert_exits("is damaged")
        with open(self.arc, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertTrue(os.path.exists(os.path.join(self.src, "f0.txt")))

    def test_second_run_at_the_same_time_is_refused(self):
        with nestzip.single_instance(self.arc + nestzip.SIDECAR + ".lock"):
            self.assert_exits("already working")
        self.assertEqual(os.listdir(self.tmp.name), ["data"])

    def test_bad_paths_and_sizes(self):
        self.assert_exits("whole drive", "check", os.path.abspath(os.sep))
        self.assert_exits("can't be inside", "pack", self.src, "-o", os.path.join(self.src, "x.zip"))
        self.assert_exits("doesn't exist", "pack", self.src, "-o", os.path.join(self.tmp.name, "no", "x.zip"))
        self.assert_exits("not a folder", "pack", os.path.join(self.tmp.name, "nope"))
        self.assert_exits(2, "pack", self.src, "--batch", "lots")
        self.assert_exits(2, "pack", self.src, "--keep-free", "-1GB")
        self.assertEqual(nestzip.parse_size("1.5g"), 3 << 29)
        self.assertEqual(nestzip.parse_size("256 MB"), 256 << 20)

    # Links must never lead the run outside the folder

    def test_links_are_never_followed(self):
        outside = os.path.join(self.tmp.name, "outside")
        os.makedirs(outside)
        precious = os.path.join(outside, "precious.txt")
        with open(precious, "w") as f:
            f.write("not part of the folder")
        link = os.path.join(self.src, "link")
        make_link(outside, link)

        self.pack()
        self.assertTrue(os.path.exists(precious), "a file outside the folder was deleted")
        self.assertTrue(os.path.isdir(link), "the link itself is left alone")
        with zipfile.ZipFile(self.arc) as z:
            self.assertFalse([n for n in z.namelist() if n.startswith("link")])
            for rel, data in self.files.items():
                self.assertEqual(z.read(rel), data, rel)

    @unittest.skipIf(os.name == "nt", "creating symlinks needs admin rights on Windows")
    def test_planted_links_next_to_the_archive(self):
        """Someone who can write next to the archive must not make nestzip write elsewhere."""
        victim = os.path.join(self.tmp.name, "victim")
        with open(victim, "w") as f:
            f.write("a system file")
        nologin = os.path.join(self.tmp.name, "nologin")
        os.symlink(victim, self.arc + nestzip.SIDECAR + ".tmp")
        os.symlink(nologin, self.arc + nestzip.SIDECAR + ".lock")

        self.assert_exits("nestzip:")
        self.assertFalse(os.path.exists(nologin), "the lock followed a link")
        os.remove(self.arc + nestzip.SIDECAR + ".lock")
        self.pack()
        with open(victim) as f:
            self.assertEqual(f.read(), "a system file", "the sidecar was written through a link")
        with zipfile.ZipFile(self.arc) as z:
            self.assertIsNone(z.testzip())

    def test_unportable_names_are_left_alone(self):
        self.assertEqual(nestzip.name_problem("a\\b.txt"), "name contains a backslash")
        self.assertEqual(nestzip.name_problem("bad\udcff.txt"), "name isn't valid UTF-8")
        self.assertIsNone(nestzip.name_problem("dossier/été 😀.txt"))

    def test_delete_refused_outside_the_folder(self):
        """A folder swapped for a link after the scan must not lead to a delete outside."""
        outside = os.path.join(self.tmp.name, "outside.txt")
        with open(outside, "w") as f:
            f.write("keep me")
        with self.assertRaises(nestzip.NestzipError):
            nestzip.remove(outside, os.path.realpath(self.src))
        self.assertTrue(os.path.exists(outside))


if __name__ == "__main__":
    unittest.main()
