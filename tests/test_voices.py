import json
import logging
import os
import tempfile
import unittest

import voices
from voices import BadVoiceError, discover_voices, resolve_voice


def write_voice(d, stem, cfg=None, size=2048, raw_json=None, with_json=True):
    with open(os.path.join(d, stem + ".onnx"), "wb") as f:
        f.write(b"\0" * size)
    if with_json:
        with open(os.path.join(d, stem + ".onnx.json"), "w") as f:
            f.write(raw_json if raw_json is not None else json.dumps(
                cfg if cfg is not None else {"audio": {"sample_rate": 22050, "quality": "medium"},
                                             "espeak": {"voice": "en-us"}, "num_speakers": 1}))


class DiscoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name

    def discover(self):
        # discovery logs at DEBUG; assertLogs keeps the output pristine and proves skips are logged
        with self.assertLogs("voices", "DEBUG"):
            logging.getLogger("voices").debug("marker")
            return discover_voices(self.d)

    def test_valid_entry_fields_and_sorting(self):
        write_voice(self.d, "en_US-zed-high", {"audio": {"sample_rate": 22050, "quality": "high"},
                                               "espeak": {"voice": "en-us"}}, size=3 * 1024 * 1024)
        write_voice(self.d, "en_GB-alba-medium")
        write_voice(self.d, "xx_YY-bare-low", {"audio": {"sample_rate": 16000}})
        got = self.discover()
        self.assertEqual([v["name"] for v in got], ["en_GB-alba-medium", "en_US-zed-high", "xx_YY-bare-low"])
        zed = got[1]
        self.assertEqual(zed, {"name": "en_US-zed-high", "sample_rate": 22050, "quality": "high",
                               "language": "en-us", "size_mb": 3.0})
        bare = got[2]  # quality and language fall back to the stem
        self.assertEqual((bare["quality"], bare["language"], bare["sample_rate"]), ("low", "xx_YY", 16000))

    def test_skips_missing_json_bad_json_and_missing_keys(self):
        write_voice(self.d, "nojson", with_json=False)
        write_voice(self.d, "badjson", raw_json="{not json")
        write_voice(self.d, "notdict", raw_json="[1,2]")
        write_voice(self.d, "norate", {"audio": {}})
        write_voice(self.d, "strrate", {"audio": {"sample_rate": "22050"}})
        write_voice(self.d, "boolrate", {"audio": {"sample_rate": True}})
        write_voice(self.d, "badspk", {"audio": {"sample_rate": 22050}, "num_speakers": "many"})
        write_voice(self.d, "good")
        self.assertEqual([v["name"] for v in self.discover()], ["good"])

    def test_skips_multi_speaker(self):
        write_voice(self.d, "multi", {"audio": {"sample_rate": 22050}, "num_speakers": 904})
        write_voice(self.d, "single", {"audio": {"sample_rate": 22050}, "num_speakers": 1})
        self.assertEqual([v["name"] for v in self.discover()], ["single"])

    def test_skips_bad_stems(self):
        for stem in ("has space", "semi;colon", "unié", ".hidden", "x" * 101):
            write_voice(self.d, stem)
        write_voice(self.d, "ok-1.2+x_y")
        self.assertEqual([v["name"] for v in self.discover()], ["ok-1.2+x_y"])

    def test_ignores_non_files_and_stray_json(self):
        os.mkdir(os.path.join(self.d, "dir.onnx"))
        with open(os.path.join(self.d, "only.onnx.json"), "w") as f:
            f.write("{}")
        self.assertEqual(self.discover(), [])

    def test_missing_dir_is_empty_not_error(self):
        with self.assertLogs("voices", "DEBUG"):
            self.assertEqual(discover_voices(os.path.join(self.d, "nope")), [])

    def test_oversized_json_skipped(self):
        write_voice(self.d, "huge", raw_json=" " * (voices.MAX_JSON_BYTES + 1))
        self.assertEqual(self.discover(), [])

    def test_skips_symlinks(self):
        write_voice(self.d, "real")
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        write_voice(other.name, "target")
        os.symlink(os.path.join(other.name, "target.onnx"), os.path.join(self.d, "symlinked.onnx"))
        os.symlink(os.path.join(other.name, "target.onnx.json"), os.path.join(self.d, "symlinked.onnx.json"))
        self.assertEqual([v["name"] for v in self.discover()], ["real"])


class SymlinkSafetyTests(unittest.TestCase):
    """REAL symlinks in temp dirs; discover_voices and resolve_voice share one validation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = os.path.join(self.tmp.name, "models")
        self.out = os.path.join(self.tmp.name, "outside")
        os.mkdir(self.d)
        os.mkdir(self.out)
        write_voice(self.d, "good")
        write_voice(self.out, "ext")

    def names(self):
        return [v["name"] for v in discover_voices(self.d)]

    def assert_rejected(self, name):
        self.assertNotIn(name, self.names())
        with self.assertRaises(ValueError) as cm:
            resolve_voice(self.d, name)
        self.assertNotIn(self.tmp.name, str(cm.exception))
        self.assertNotIn(os.sep, str(cm.exception))

    def test_control_good_voice_is_accepted_by_both(self):
        self.assertEqual(self.names(), ["good"])
        self.assertEqual(str(resolve_voice(self.d, "good")[0]), os.path.join(self.d, "good.onnx"))

    def test_symlinked_onnx_alone(self):
        write_voice(self.d, "linkonnx", with_json=True)
        os.unlink(os.path.join(self.d, "linkonnx.onnx"))
        os.symlink(os.path.join(self.out, "ext.onnx"), os.path.join(self.d, "linkonnx.onnx"))
        self.assertTrue(os.path.islink(os.path.join(self.d, "linkonnx.onnx")))
        self.assertFalse(os.path.islink(os.path.join(self.d, "linkonnx.onnx.json")))
        self.assert_rejected("linkonnx")

    def test_symlinked_json_alone(self):
        write_voice(self.d, "linkjson")
        os.unlink(os.path.join(self.d, "linkjson.onnx.json"))
        os.symlink(os.path.join(self.out, "ext.onnx.json"), os.path.join(self.d, "linkjson.onnx.json"))
        self.assertFalse(os.path.islink(os.path.join(self.d, "linkjson.onnx")))
        self.assert_rejected("linkjson")

    def test_symlink_to_file_inside_models_dir_is_still_rejected(self):
        os.symlink(os.path.join(self.d, "good.onnx"), os.path.join(self.d, "alias.onnx"))
        os.symlink(os.path.join(self.d, "good.onnx.json"), os.path.join(self.d, "alias.onnx.json"))
        self.assert_rejected("alias")
        self.assertEqual(self.names(), ["good"])

    def test_entry_pointing_outside_models_dir(self):
        os.symlink(os.path.join(self.out, "ext.onnx"), os.path.join(self.d, "ext.onnx"))
        os.symlink(os.path.join(self.out, "ext.onnx.json"), os.path.join(self.d, "ext.onnx.json"))
        self.assert_rejected("ext")

    def test_containment_check_is_independent_of_the_symlink_check(self):
        """Regular files whose realpath is not under the given models realpath are refused."""
        other = os.path.join(self.tmp.name, "elsewhere")
        os.mkdir(other)
        self.assertIsNotNone(voices._checked_files(self.d, os.path.realpath(self.d), "good"))
        self.assertIsNone(voices._checked_files(self.d, os.path.realpath(other), "good"))

    def test_symlinked_models_dir_itself_is_allowed(self):
        link = os.path.join(self.tmp.name, "models-link")
        os.symlink(self.d, link)
        self.assertEqual([v["name"] for v in discover_voices(link)], ["good"])
        self.assertEqual(str(resolve_voice(link, "good")[0]), os.path.join(link, "good.onnx"))

    def test_symlinked_subpath_component_inside_models_dir(self):
        """A voice reached through a directory symlink cannot be addressed by name at all."""
        os.symlink(self.out, os.path.join(self.d, "dirlink"))
        self.assertEqual(self.names(), ["good"])
        with self.assertRaises(ValueError):
            resolve_voice(self.d, "dirlink/ext")


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name
        write_voice(self.d, "en_US-amy-medium")
        os.mkdir(os.path.join(self.d, "sub"))
        write_voice(os.path.join(self.d, "sub"), "inner")

    def test_resolves_installed(self):
        onnx, js = resolve_voice(self.d, "en_US-amy-medium")
        self.assertEqual(str(onnx), os.path.join(self.d, "en_US-amy-medium.onnx"))
        self.assertEqual(str(js), str(onnx) + ".json")

    def test_rejects_everything_else(self):
        bad = ["../x", "a/b", "/etc/passwd", os.path.join(self.d, "en_US-amy-medium"), "sub/inner",
               "..", ".", "", "unknown", "en_US-amy-medium.onnx", "en_US-amy-medium\n", None, 5, ["x"], "x" * 101]
        for name in bad:
            with self.subTest(name=name), self.assertRaises(ValueError):
                resolve_voice(self.d, name)

    def test_is_value_error_subclass(self):
        self.assertTrue(issubclass(BadVoiceError, ValueError))


if __name__ == "__main__":
    unittest.main()
