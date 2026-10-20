These terminal captures are real `wavecoord` output from a throwaway demo repo, driven by the fakes in `tests/` (no model, no network).

| Files | What | Made by |
|---|---|---|
| `*.svg` | Static terminal screenshots | `scripts/docs-screens.py` |
| `anim-*.svg` | The same terminal, animated with CSS (typed commands, then their output); they loop, and show the last frame where animation is off | `scripts/docs_video.py` |
| `wave.gif`, `plan.gif`, `steer.gif` | GIFs of three of the animations, for viewers that do not animate SVG | `scripts/docs_video.py` |

The narrated videos (`quickstart.mp4`, `spec-to-merge.mp4`, `steering.mp4`, each with an `.srt`) are not in git. They are attached to the GitHub release.

## Regenerate

Everything, in one command (about a minute):

```bash
python3 scripts/docs-screens.py --all
```

This runs the demo once, writes the static SVGs, the animated SVGs and the GIFs here, and the MP4s and `.srt` files to `dist/videos/` (gitignored; `--video-out DIR` to change it). Then upload the videos to the release:

```bash
gh release upload v0.3.0 dist/videos/*.mp4 dist/videos/*.srt --clobber
```

Parts:

| Command | Writes | Needs |
|---|---|---|
| `python3 scripts/docs-screens.py` | static SVGs | Python, git |
| `... --anim` | `anim-*.svg` | nothing more |
| `... --gif` | `*.gif` | `rsvg-convert` (librsvg), `ffmpeg` |
| `... --video` | `dist/videos/*.mp4`, `*.srt` | `rsvg-convert`, `ffmpeg`, and a voice: macOS `say` (default, voice Samantha) |

Other options: `--only NAME` (one animation or video, repeatable), `--voice NAME`, `--tts openai` (needs `OPENAI_API_KEY`) or `--tts elevenlabs` (needs `ELEVENLABS_API_KEY`), `--scenes FILE` to save the recorded scenes, and `--from-scenes FILE` to rebuild animations and videos from them without the demo. `--out DIR` writes the images elsewhere and `--keep` keeps the demo repo. The script stops with a clear message when a tool it needs is missing.

The narration is in `scripts/narration/<video>.txt`, one `[section]` per chapter. Each sentence is spoken on its own and becomes one subtitle, and each chapter lasts at least as long as its narration, so the voice and the picture stay in sync. Spoken audio is cached in `dist/videos/.tts-cache/`.
