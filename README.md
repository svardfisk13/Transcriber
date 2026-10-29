# Transcriber

Long-form speech transcription with Whisper, from the command line.

A command-line tool that turns an audio or video recording into text. It is
built for hour-long material - meetings, interviews, talks - where the usual
"chop into 30-second windows" approach drifts, repeats itself, and hallucinates
`Thank you.` into every silent passage.

- **No ffmpeg install.** Audio is decoded in-process by PyAV, which ships
  ffmpeg's libraries inside the wheel. An `ffmpeg` on `PATH` is used only as a
  fallback for containers PyAV cannot open.
- **Swedish, properly.** KBLab's `kb-*` models are wired in and beat OpenAI's
  equivalents on Swedish by a wide margin.
- **Guards against hallucination.** Temperature fallback, a compression-ratio
  degeneracy check, and chunk planning that never pads a trailing sliver of
  speech out to a full window with silence.
- **Names come out right.** `--vocab` biases decoding toward the people,
  products and jargon in your recording.
- **Records the meeting too.** `transcribe record` captures your microphone and
  the other side of a Teams call on Windows, with no virtual cable or driver.

## Install

Python 3.9 or newer. No ffmpeg, and no model downloads at install time.

**To use it** - `pipx` gives the tool its own isolated environment and puts a
`transcribe` command on your PATH, usable from any folder:

```powershell
py -m pip install --user pipx
pipx install git+https://github.com/svardfisk13/Transcriber
```

`pipx reinstall transcriber` later fetches the latest code from GitHub.

**To work on it** - an editable install, so your edits take effect immediately:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -e .
```

`transcribe` is then available while the venv is active (the prompt shows
`(.venv)`), from any folder. It runs the code of whichever branch is checked
out, so switching to a branch switches the tool with it.

Either way the dependencies come down automatically. `pip install -r
requirements.txt` still works and does the same as the editable install.

The optional backend is a separate, much larger install:
`pip install -e .[transformers]`, plus `.[vad]` for Silero VAD.

> Model weights are **not** installed. The one you pick is downloaded on first
> use and cached in `~/.cache/huggingface`, shared by every environment on the
> machine - so the first run of a given model is slow and the rest are instant.
> The default `large-v3-turbo` is 1.6 GB; `-m tiny` (75 MB) is a quick way to
> check an install works.

## Quick start

```powershell
transcribe meeting.m4a
```

Without installing, from a clone: `python -m transcriber meeting.m4a`.

Run with no `-m`, `-l` or `--vad` and it asks which model, which language and
whether to skip silence, then writes `meeting.txt` and `meeting.json` next to
the recording. With stdin closed (a scheduled task) each question takes its
default instead of stopping.

```powershell
# pick everything up front, no prompts
transcribe -m large-v3-turbo --vad off meeting.m4a

# Swedish interview, subtitles as well
transcribe -m kb-medium -l sv --formats txt,srt intervju.mp3

# several files in one load of the model. PowerShell does not expand globs
# for native commands, so let Get-ChildItem do it:
transcribe -m kb-large -l sv (gci *.m4a)
```

Progress, timings and warnings go to stderr; only the output files are written
to disk, so you can redirect one without losing the other.

## Models

`-m` takes any name below, or a full Hugging Face id (`KBLab/kb-whisper-large`).
Weights download on first use and are cached by `huggingface_hub`.

| Name | Language | Notes |
| --- | --- | --- |
| `tiny`, `base`, `small`, `medium` | multilingual | increasing size and accuracy |
| `large-v3` | multilingual | most accurate, slowest |
| `large-v3-turbo` | multilingual | **default** - near `large-v3` accuracy, much faster |
| `distil-large-v3` | English only | fast, English-only |
| `kb-tiny`, `kb-base`, `kb-small` | Swedish | `kb-small` beats OpenAI `small` by a wide margin |
| `kb-medium` | Swedish | better than `large-v3`, at a third the size |
| `kb-large` | Swedish | best available |

The `kb-*` repos carry CTranslate2 weights, so faster-whisper loads them
directly - no conversion step.

> Omit `-l` and you are asked. The offered default follows the model - `sv` for
> the `kb-*` models, `en` otherwise - so picking `kb-large` from the menu no
> longer quietly translates your Swedish into English. Pass `-l sv`, `-l auto`,
> or any other ISO code to skip the question.

## Output

`--formats` is a comma-separated list; the default is `txt,json`.

| Format | Contents |
| --- | --- |
| `txt` | flowing text with `[hh:mm:ss]` markers every minute (`--no-minute-markers` to omit) |
| `srt` | subtitles, `hh:mm:ss,mmm` |
| `vtt` | WebVTT subtitles |
| `json` | timestamped segments, full text, and the exact settings and timings of the run |

Files are named after the recording and land beside it, or in `-o <dir>`.

`--folder` (`-f`) instead gives each recording a folder of its own, named after
it, and moves the recording in with its transcripts:

```powershell
transcribe --folder recording01.m4a
```

```
recording01\recording01.m4a
recording01\recording01.txt
recording01\recording01.json
```

The recording moves only once its transcripts are written, so a failed run
leaves it where it was. A `recording01.vocab.txt` moves with it; a shared
`vocab.txt` stays put. Run `--folder` again on `recording01\recording01.m4a` and
the transcripts are rewritten in place, not nested a level deeper. `--folder`
cannot be combined with `-o`.

Note that `.gitignore` excludes `*.txt`, `*.json`, `*.srt` and `*.vtt` so
transcripts never get committed by accident.

## Recording a meeting

`transcribe record` records a Teams call (or anything else) from your own
machine: your microphone, and whatever plays through your speakers or headset.
Windows lets programs capture the latter directly (WASAPI loopback), so there is
no virtual cable or driver to install.

```powershell
transcribe record                # until Ctrl+C; named by date, e.g. 2026-10-14_1400.opus
transcribe record standup        # standup.opus
transcribe record standup -o D:\Minutes   # into another folder, created if missing
transcribe --folder D:\Minutes\standup.opus   # then transcribe it as usual
```

Without `-o` the recording lands in the folder the terminal is in. Both the
start and the end of a run print where it is, and the end prints the command
to transcribe it. A run that records less than a second - a device that would
not open, or Ctrl+C straight away - leaves no file behind.

While it runs, a status line shows the time and a level meter for each side:
`mic` is you, `call` is everyone else. Each bar fills as that side gets louder.
An empty `call` bar while people are talking means the wrong output is being
recorded.

### Picking the microphone and speakers

By default it records the Windows default microphone and speakers. If Teams
talks to a headset, point it there. The choice is remembered (in
`%APPDATA%\transcriber\devices.json`), so this is once per device:

```powershell
transcribe record --list-devices
transcribe record --mic 2 --speaker Jabra   # a number from the list, or part of a name
transcribe record --mic default             # back to the Windows default
```

Change one and the other stays as it was. A remembered device that is not
connected - headset left at home - is skipped with a note, and the Windows
default is used for that recording only. Prefer names to numbers: the numbers
shift as devices come and go.

Check **Teams > Settings > Devices**: record the same speaker and microphone
Teams is using.

**Bluetooth headsets (AirPods and the like).** Pick the headset for both:

```powershell
transcribe record --mic AirPods --speaker AirPods
```

Windows 11 lists it once in each list - a `Headset` microphone and a
`Headphones` output - and switches it into call mode by itself when the
microphone is in use. Older Windows lists the call mode as a separate
*Hands-Free* device; pick that one for both, since the stereo device goes
silent during a call. Call mode is narrowband, roughly phone quality, which
costs the transcript very little. Connect the headset before you start: if it
disconnects mid-meeting, the recording stops and keeps what it has.

### Back-to-back meetings

Recording and transcribing are separate steps on purpose. Transcription is
heavy on the CPU, and running it during the next call could hurt Teams and
make the recorder drop audio. Record all day, then transcribe everything in
one go, with one load of the model:

```powershell
transcribe --folder -m kb-large -l sv --vad off (gci D:\Minutes\*.opus)
```

`--folder` moves each finished recording into a folder of its own, so any
`.opus` still loose in `D:\Minutes` is one not yet transcribed, and the same
command next time picks up exactly what is left.

### The file

Stereo Opus, about 22 MB an hour: your microphone on the left, the call on the
right. Transcription mixes them back down to one. It is written as it goes, so
a crash or a closed window loses only the last second or so. Windows' Media
Player, VLC and browsers play it; some other apps, Apple's in particular, may
not. `.gitignore` excludes `*.opus`, so a recording made inside the project is
never committed.

### Worth knowing

- **Use headphones.** On laptop speakers your microphone picks up the call as
  well, so the other side is recorded twice, slightly apart. Teams removes that
  echo from what it sends, but not from what this records.
- **Teams' mute does not mute this.** Mute stops Teams sending your voice; the
  microphone itself keeps recording.
- **Tell people.** Unlike Teams' own recording, nobody else in the call is
  shown that you are recording.

## Vocabulary

Whisper mangles names it has never seen. Give it a list and they come out right:

```powershell
transcribe --vocab "EPAM, AstraZeneca, Kubernetes" meeting.m4a
transcribe --vocab @terms.txt meeting.m4a
```

With no `--vocab`, it looks for `<recording>.vocab.txt` and then
`vocab.txt` beside the audio, and uses the first it finds. One term per line,
saved as UTF-8; `#` starts a comment. It must be named exactly that: other
`.txt` files beside the audio - transcripts among them - are not read. Terms
are passed as faster-whisper *hotwords*, so they bias every window rather than
just the first - this needs the faster-whisper backend and is ignored
elsewhere.

A `vocab.txt` in the folder you record into covers every meeting there. One
catch with `--folder`: the recording moves into a subfolder but a shared
`vocab.txt` stays put, so to transcribe it again later, name the file:

```powershell
transcribe --vocab "@D:\Minutes\vocab.txt" "D:\Minutes\standup\standup.opus"
```

A `<recording>.vocab.txt` moves with its recording and has no such catch.

## Options

| Flag | Default | What it does |
| --- | --- | --- |
| `-m`, `--model` | ask, else `large-v3-turbo` | model name or Hugging Face id |
| `-l`, `--language` | ask, else `sv` for `kb-*` / `en` | ISO code, or `auto` to detect |
| `--task` | `transcribe` | or `translate`, to English |
| `--backend` | `auto` | `faster-whisper` or `transformers` |
| `--device` | `auto` | `cpu` or `cuda` |
| `--compute-type` | `auto` | faster-whisper precision: `int8`, `float16`, `float32`, ... |
| `--beams` | `5` | `1` is fastest, `5` more accurate |
| `--vad` | ask, else `off` | drop non-speech before decoding |
| `--vad-min-silence` | `400` | ms of silence that splits speech |
| `--vocab` | auto-detect | terms to bias toward, inline or `@file` |
| `--condition-on-previous` | `off` | feed earlier text back as context |
| `--formats` | `txt,json` | `txt`, `srt`, `vtt`, `json` |
| `-o`, `--output-dir` | beside the input | where to write |
| `-f`, `--folder` | off | move each recording into a folder named after it, with its transcripts |
| `--no-minute-markers` | off | omit `[hh:mm:ss]` markers from the `.txt` |

`transcribe record` takes its own options (`transcribe record -h`):

| Flag | Default | What it does |
| --- | --- | --- |
| `name` | date and time | file name; `.opus` is added |
| `-o`, `--output-dir` | the current folder | where to write |
| `--mic` | remembered, else Windows default | number from `--list-devices`, part of a name, or `default` |
| `--speaker` | remembered, else Windows default | the output to record the call from; same forms as `--mic` |
| `--list-devices` | | show microphones and speakers, then exit |

`--mode`, `--batch-size`, `--chunk-s` and `--overlap-s` apply to the
transformers backend only; `transcribe -h` covers them. `--chunk-s`
cannot exceed 30 - Whisper's receptive field is fixed at that length, and a
longer window would be truncated with the remainder silently discarded.

### Silence filtering

`--vad off` is the default and the right choice for meetings: every quiet
passage is still decoded, so a soft-spoken participant is not dropped.
`--vad on` skips silence and runs faster, at the risk of clipping quiet
speakers. `--vad auto` means on for faster-whisper, off for transformers.

### Backends

`auto` picks **faster-whisper** whenever it is installed and the model has a
CTranslate2 build - roughly 4x faster than transformers on CPU at the same
accuracy, with a progress bar and an ETA in realtime multiples.

**transformers** is the fallback, in two modes. `longform` (default) uses
Whisper's native sequential long-form decoding. `chunked` windows the audio
explicitly, optionally cutting on silence with Silero VAD (`--vad on`, which
needs the `vad` extra), and de-duplicates the overlap between windows.

## Troubleshooting

**It says "produced no text".** Usually `--vad on` on a quiet recording. Rerun
with `--vad off`.

**The transcript is in English but the audio isn't.** The language question
took its default. Pass `-l sv` (or the right ISO code, or `-l auto`) explicitly
- this happens most easily in a scheduled task, where every prompt is answered
by its default.

**"PyAV could not decode".** It falls back to an `ffmpeg` on `PATH` if
there is one; otherwise convert the file to `.wav` first.

**`OMP: Error #15`.** torch and ctranslate2 each bundle an Intel OpenMP
runtime. Transcriber avoids importing both, so this points at something else
in the environment loading torch first - a `pipx` install cannot hit this,
since it gets an environment of its own.

**It looks hung on a long file.** Transformers long-form decoding exposes no
per-window hook, so it prints an elapsed-time heartbeat every minute instead of
a bar. The default faster-whisper backend shows a real progress bar.

**`transcribe` is not recognised.** With the editable install, activate the
venv first: `.\.venv\Scripts\Activate.ps1` in the project folder. The `pipx`
install puts it on your PATH, but an open terminal will not pick that up until
you restart it; `pipx ensurepath` fixes a PATH that never got set up.

**The `call` meter stays empty.** The call is playing through a different output
than the one being recorded. Compare `transcribe record --list-devices` with
Teams > Settings > Devices, and pick the right one with `--speaker`.

**"the microphone has recorded pure silence".** Windows hands out silence rather
than an error when its privacy settings block desktop apps from the microphone:
check Settings > Privacy & security > Microphone. Otherwise the wrong
microphone is picked; see `--list-devices`.

**"recording stopped: <device>".** The device went away mid-recording -
unplugged, or a Bluetooth headset out of range or out of battery. What was
recorded up to then is saved; start a new recording to carry on.

**"transcripts written, but could not move".** With `--folder`, something held
the recording open at the moment it was moved - often OneDrive syncing it. The
transcripts are in the new folder; move the recording in by hand.

## License

MIT - see [LICENSE](LICENSE).
