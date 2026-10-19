# Course recording rig

`rig.py` records a real, scripted Claude Code session for the video course. It
types into a live `claude` and waits on what the screen shows. It records only
the terminal window, then adds the narration (`say`) and subtitles.

```bash
COURSE_DEMO_DIR=/tmp/wavecoord-demo \
  python3 scripts/course/rig.py run scripts/course/proof-take.json --out /tmp/course-out/proof
```

Outputs in `--out`:

- `<lesson>.mp4`: the final video, 1920x1080, 30 fps, H.264 + AAC, with soft English subtitles.
- `<lesson>.srt`: the subtitles as a sidecar file.
- `timeline.json`: each step with its start and end time in the video.
- `usage.json`: the tokens Claude Code used (from its transcripts), with the API list price.
- `raw.mp4` (the cropped capture without audio), `voice/` (one narration clip per line), `ctl/` (driver state, the screen text).

## How it works

1. Each `say` line is rendered with `say -v Samantha -o`, so its length is known in advance.
2. A dedicated Terminal.app window opens through AppleScript, with the profile
   `wavecoord-course` (Menlo 20, dark background), at fixed bounds (default
   1536x864 at 16,64). The rig's driver runs in it.
3. The driver starts the lesson command in a pty sized to the window. It mirrors
   the pty to the window and runs the `setup` steps off camera, for example the
   folder-trust dialog.
4. ffmpeg (avfoundation) records the screen, cropped to the window before
   encoding. No full-screen frame is ever written. The crop also drops the title
   bar and the rounded corners (`inset`).
5. The driver plays `steps` and logs a timestamp for each one.
6. The rig places each voice clip at its step's time (`adelay` + `amix`), writes
   the .srt and centers the window on a 1920x1080 canvas without scaling it.

## Lesson format (JSON)

```json
{
  "cwd": "$COURSE_DEMO_DIR",
  "command": ["claude", "--model", "haiku", "--permission-mode", "default"],
  "prepare": ["git reset --hard -q"],
  "env": {},
  "config": {"font_size": 20, "rate": 180},
  "setup": [ ...steps run before recording... ],
  "steps": [ ...recorded steps... ]
}
```

`cwd` expands `~` and `$VARS`, and a relative path is resolved from the lesson
file. `--cwd` overrides it. `prepare` holds shell commands that run in `cwd`
before anything else. `config` overrides `DEFAULTS` in `rig.py`: `voice`, `rate`,
`font`, `font_size`, `bounds`, `inset`, `canvas`, `type_cps`, `lead_in`, `tail`,
`say_gap` and the colors.

Steps:

| Step | What it does |
|---|---|
| `{"say": "text"}` | Narration starts here. By default the driver waits for the line to finish before the next step. `"wait": false` lets the next step run while the voice plays. |
| `{"type": "text", "enter": true}` | Types the text at `cps` characters per second (default 16), then presses Return. |
| `{"key": "escape"}` | Sends a key: `enter`, `escape`, `tab`, `shift-tab`, `up`, `down`, `left`, `right`, `backspace`, `space`, `ctrl-c`, `ctrl-o`, `ctrl-t`, and so on. `"repeat": n` sends it n times. |
| `{"wait_for": "regex", "timeout": 60, "idle": 2}` | Polls the screen text printed since the last `type` or `key` step (ANSI stripped) until the regex matches. With `idle`, it then waits until the screen has been still for that many seconds. It fails the take on timeout unless `"optional": true`. |
| `{"idle": 2, "timeout": 60}` | Waits until the screen has been still for N seconds. |
| `{"pause": 2}` | Waits N seconds. |
| `{"shell": "cmd"}` | Runs a command off camera (in `cwd` unless `"cwd"` is given) and logs its output. Use it to set up state between beats. |
| `{"wait_exit": 10}` | Waits up to N seconds for the session to exit. |

Any step can take `"when": "regex"`. The step then runs only if the screen text
since the last action matches. This handles dialogs that appear only sometimes.

## Writing lessons for the rig

- Put dialogs Claude Code might show at startup in `setup`, guarded with `when`:
  folder trust (this version puts "No, exit" first, so the step is `down`, then
  `enter`), onboarding, release notes.
- Start Claude with `--permission-mode default`. In auto mode, Claude Code can
  pop "Teach auto mode about your environment?", which swallows the next
  keystrokes. In one take it turned `/exit` into `/auto-mode-setup`.
- After each prompt, use `wait_for` with something specific to the answer (a
  file name, "Worked for"), plus `idle` of 2–3 s. The spinner keeps the screen
  busy while Claude works, so idle means it is done.
- Narrate after the result appears, not over a variable wait. To talk while
  Claude works, use `"wait": false`, and keep the line shorter than the fastest
  answer.
- Keep prompts small and deterministic, and run them against a demo repo you can
  reset with `prepare`. Answers vary between takes, so keep narration generic
  ("Claude lists the files") rather than quoting exact output.
- Put the demo repo at a short, neutral path such as `/tmp/wavecoord-demo`.
  Claude Code's header shows the working directory, and a path under your home
  directory shows your user name.
- Typing `/` opens the command menu, which lists your own installed skills and
  plugins. Type full commands without pausing, or use a clean
  `CLAUDE_CONFIG_DIR` set up for recording.

## One-time setup

- **Screen recording** for the app that runs the rig (the terminal or IDE that
  starts `rig.py`): System Settings > Privacy & Security > Screen & System Audio
  Recording.
- **Automation** of Terminal for the same app. Granted on first use if macOS
  asks; no prompt appeared on the machine this was built on.
- The rig adds a Terminal profile named `wavecoord-course`. Change it in
  Terminal > Settings > Profiles, or through `font`, `font_size` and the color
  keys in `config`.
- Before recording, turn on Do Not Disturb. Banners appear at the top right,
  outside the default window bounds, but anything that pops up over the window
  is recorded.

## Limitations

- The recording is the live screen region. Keep the window in front and don't
  touch it during a take. Keystrokes typed into the window go nowhere (the
  driver puts its tty in raw mode), but other windows moved over it would show.
- Bounds assume a 1920x1080, non-Retina display with the Dock at the bottom. On
  a Retina display the scale factor is detected from `system_profiler`, or set
  `--scale-factor`.
- Video time zero is estimated as the time ffmpeg was stopped minus the clip
  length. Voice onsets in the proof take matched the step log to within 10 ms.
- Claude Code's TUI changes between versions. The `wait_for` regexes are what
  breaks first.
