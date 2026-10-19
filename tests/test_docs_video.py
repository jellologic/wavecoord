"""The pure parts of scripts/docs_video.py: timeline, frames, animated SVG, narration and subtitles.
No demo, no ffmpeg, no rsvg-convert, no TTS."""

import argparse
import importlib.util
import os
import re
import sys
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

import _paths

SVG = "{http://www.w3.org/2000/svg}"


def load():
    if "docs_video" in sys.modules:
        return sys.modules["docs_video"]
    spec = importlib.util.spec_from_file_location("docs_video", os.path.join(_paths.ROOT, "scripts", "docs_video.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["docs_video"] = mod
    spec.loader.exec_module(mod)
    return mod


STEPS = [
    {"cmd": "wavecoord status", "out": "ok: http://127.0.0.1:18741\n", "secs": 0.2},
    {"cmd": "wavecoord spec waves", "out": "\n".join("wave %d (1):  t%d" % (i, i) for i in range(1, 6)) + "\n",
     "secs": 1.4},
    {"cmd": "wavecoord hint t1 'go <on> & on'", "out": "\x1b[32mhint #1\x1b[0m sent\n", "secs": 0.3},
]


class TimelineTest(unittest.TestCase):
    def setUp(self):
        self.v = load()

    def test_types_every_character_then_shows_output(self):
        ev, total = self.v.timeline(STEPS)
        times = [t for t, _ in ev]
        self.assertEqual(times, sorted(times))
        self.assertEqual(ev[0], (0.0, (0, 0, 0)))
        self.assertEqual(ev[-1][1], (3, 0, 0))  # the idle prompt
        self.assertGreater(total, ev[-1][0])
        typed = [s[1] for _, s in ev if s[0] == 0 and s[2] == 0]
        self.assertEqual(typed, list(range(len(STEPS[0]["cmd"]) + 1)))
        # a fast command shows its output at once, a slow one line by line
        self.assertEqual([s[2] for _, s in ev if s[0] == 0 and s[2]], [1])
        self.assertEqual([s[2] for _, s in ev if s[0] == 1 and s[2]], [1, 2, 3, 4, 5])

    def test_extra_time_lengthens_the_pauses_after_output(self):
        ev, total = self.v.timeline(STEPS)
        ev2, total2 = self.v.timeline(STEPS, extra=9.0)
        self.assertAlmostEqual(total2 - total, 9.0)
        self.assertEqual(ev[:len(STEPS[0]["cmd"]) + 2], ev2[:len(STEPS[0]["cmd"]) + 2])  # step 1 runs as early

    def test_offset_continues_earlier_steps(self):
        ev, _ = self.v.timeline(STEPS[1:], offset=1)
        self.assertEqual(ev[0][1], (1, 0, 0))
        self.assertEqual(ev[-1][1], (3, 0, 0))

    def test_frames_are_deduplicated_and_add_up(self):
        ev, total = self.v.timeline(STEPS)
        fr = self.v.frames(ev, total, fps=10)
        self.assertEqual(fr[0][0], (0, 0, 0))
        self.assertEqual(fr[-1][0], (3, 0, 0))
        self.assertTrue(all(d > 0 for _, d in fr))
        self.assertTrue(all(a[0] != b[0] for a, b in zip(fr, fr[1:])))
        self.assertAlmostEqual(sum(d for _, d in fr), round(total * 10 + 0.4999) / 10.0, places=6)
        for _, d in fr:
            self.assertAlmostEqual(d * 10, round(d * 10), places=6)  # whole frames

    def test_frames_merge_events_in_one_frame(self):
        fr = self.v.frames([(0.0, "a"), (0.01, "b"), (0.5, "c"), (0.52, "c")], 1.0, fps=10)
        self.assertEqual(fr, [("b", 0.5), ("c", 0.5)])


class ScreenTest(unittest.TestCase):
    def setUp(self):
        self.v = load()

    def text(self, rows):
        return ["".join(t for t, _ in r) for r in rows]

    def test_rows_and_cursor(self):
        rows, cur = self.v.screen_rows(STEPS, (1, 5, 0), 100)
        self.assertEqual(self.text(rows), ["$ wavecoord status", "ok: http://127.0.0.1:18741", "", "$ wavec"])
        self.assertEqual(cur, (3, 7))
        rows, cur = self.v.screen_rows(STEPS, (1, len(STEPS[1]["cmd"]), 2), 100)
        self.assertEqual(self.text(rows)[-2:], ["wave 1 (1):  t1", "wave 2 (1):  t2"])
        self.assertIsNone(cur)
        rows, cur = self.v.screen_rows(STEPS, (3, 0, 0), 100)
        self.assertEqual(self.text(rows)[-2:], ["", "$ "])
        self.assertEqual(cur, (len(rows) - 1, 2))

    def test_frame_svgs_are_valid_and_scroll(self):
        width_cols, n = self.v.scene_size(STEPS, 100)
        svg = self.v.frame_svg(STEPS, (3, 0, 0), "t", 100, width_cols, n)
        root = ET.fromstring(svg)
        self.assertEqual(root.get("width"), str(self.v.window_px(width_cols, n)[0]))
        small = self.v.frame_svg(STEPS, (3, 0, 0), "t", 100, width_cols, 4)
        texts = ["".join(t.itertext()) for t in ET.fromstring(small).iter(SVG + "text")][1:]
        self.assertEqual(texts, ["$ wavecoord hint t1 'go <on> & on'", "hint #1 sent", "$ "])
        ET.fromstring(self.v.video_frame_svg(STEPS, (1, 3, 0), "~/demo", "A <heading>"))
        ET.fromstring(self.v.card_svg("Title & more", "sub"))


class AnimTest(unittest.TestCase):
    def setUp(self):
        self.v = load()

    def test_valid_looping_svg_with_the_final_frame_as_its_static_form(self):
        svg = self.v.anim_svg(STEPS, "A <demo>", cols=100)
        root = ET.fromstring(svg)
        width_cols, n = self.v.scene_size(STEPS, 100)
        self.assertEqual((int(root.get("width")), int(root.get("height"))), self.v.window_px(width_cols, n))
        self.assertEqual(root.find(SVG + "title").text, "A <demo>")
        self.assertNotIn("\x1b", svg)
        style = root.find(SVG + "style").text
        self.assertIn("infinite", style)
        self.assertIn("prefers-reduced-motion", style)
        _, total = self.v.timeline(STEPS)
        self.assertIn("animation-duration:%.2fs" % total, style)
        # every class used has its keyframes
        used = set(re.findall(r'class="a (k\d+)"', svg))
        self.assertTrue(used)
        for k in used:
            self.assertIn("@keyframes %s{" % k, style)
        # all the final rows are there once, in order
        texts = [("".join(t.itertext())) for t in root.iter(SVG + "text")][1:]
        rows, _ = self.v.screen_rows(STEPS, (3, 0, 0), 100)
        self.assertEqual(texts, [x for x in ("".join(t for t, _ in r) for r in rows) if x])
        # one typing cover per command, hidden in the static form
        covers = [g for g in root.iter(SVG + "g") if g.get("opacity") == "0"]
        self.assertEqual(len(covers), len(STEPS))

    def test_size_stays_small(self):
        steps = [{"cmd": "wavecoord cmd %d" % i, "out": "\n".join("line %d" % j for j in range(12)), "secs": 1}
                 for i in range(6)]
        self.assertLess(len(self.v.anim_svg(steps, "t")), 60000)


class NarrationTest(unittest.TestCase):
    def setUp(self):
        self.v = load()

    def test_srt(self):
        self.assertEqual(self.v.srt_time(0), "00:00:00,000")
        self.assertEqual(self.v.srt_time(3725.5), "01:02:05,500")
        self.assertEqual(self.v.srt_time(59.9996), "00:01:00,000")
        self.assertEqual(self.v.srt([(0.4, 2.0, "One."), (2.5, 4.25, "Two.")]),
                         "1\n00:00:00,400 --> 00:00:02,000\nOne.\n\n2\n00:00:02,500 --> 00:00:04,250\nTwo.\n")

    def test_parse_and_split(self):
        text = "# a comment\n[intro]\nHello there.\nIt runs.\n\n[next-one]\nspec waves shows it. README.md stays.\n"
        n = self.v.parse_narration(text)
        self.assertEqual(n, {"intro": "Hello there. It runs.", "next-one": "spec waves shows it. README.md stays."})
        self.assertEqual(self.v.sentences(n["next-one"]), ["spec waves shows it.", "README.md stays."])
        self.assertEqual(self.v.say_as("wavecoord gate-check and README.md"), "wave coord gate check and read me")

    def test_voice_layout(self):
        starts, need = self.v.voice_layout([2.0, 1.0])
        self.assertEqual(starts, [self.v.VOICE_LEAD_S, self.v.VOICE_LEAD_S + 2.0 + self.v.VOICE_GAP_S])
        self.assertAlmostEqual(need, starts[-1] + 1.0 + self.v.VOICE_TAIL_S)
        self.assertEqual(self.v.voice_layout([]), ([], 0.0))

    def test_every_chapter_has_narration(self):
        for name, (_, chapters) in self.v.VIDEOS.items():
            with open(os.path.join(self.v.NARRATION_DIR, name + ".txt")) as f:
                n = self.v.parse_narration(f.read())
            self.assertEqual(sorted(n), sorted(k for k, _, _ in chapters), name)
            self.assertTrue(all(self.v.sentences(t) for t in n.values()))

    def test_missing_tools_fail_clearly(self):
        args = argparse.Namespace(gif=False, video=True, tts="say")
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(SystemExit) as cm:
                self.v.check_tools(args)
        self.assertIn("rsvg-convert", str(cm.exception))
        self.assertIn("ffmpeg", str(cm.exception))
        args = argparse.Namespace(gif=False, video=True, tts="openai")
        with mock.patch("shutil.which", return_value="/bin/x"), mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as cm:
                self.v.check_tools(args)
        self.assertIn("OPENAI_API_KEY", str(cm.exception))
        with mock.patch("shutil.which", return_value=None):
            self.v.check_tools(argparse.Namespace(gif=False, video=False, tts="say"))  # --anim needs nothing


if __name__ == "__main__":
    unittest.main()
