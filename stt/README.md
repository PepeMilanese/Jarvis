# Offline voice memos — Whisper on your own CPU

Talk to Jarvis instead of typing: send a Telegram **voice memo**, the dispatcher downloads it, transcribes
it **on the machine** with [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CTranslate2, CPU,
int8), echoes the transcript back to you — `🎤 I heard: …` — and starts acting on it at once. No
speech-to-text service ever hears the audio (it arrives through Telegram, and only the transcript goes on to
Claude), and nothing is downloaded at run time.

```bash
python -m jarvis stt-setup                     # once: stt/.venv + pinned packages + the pinned model (~1.6 GB)
python -m jarvis stt memo.ogg                  # transcript, then one [stt: ...] line
python -m jarvis stt memo.ogg --language en    # force a language (faster; see the warning below)
```

## How the model is shipped — and why not in the repo

`model.bin` for large-v3-turbo is ~1.6 GB, far over GitHub's 100 MB file limit. So `stt-setup` downloads
it from Hugging Face — the canonical, versioned host — **pinned to the exact commit that was benchmarked
and verified against its published sha256**. A fresh install gets those exact weights or fails loudly (a
mismatch is moved aside as `model.bin.rejected`, so the transcriber reports the model missing instead of
silently using it). That is the same integrity guarantee a release asset would give, without re-hosting
someone else's weights. `stt/.venv/` and `stt/models/` are git-ignored.

No ffmpeg needed: PyAV's wheel carries the decoders. Ogg/Opus (what Telegram sends), MP3, M4A/AAC, FLAC,
WAV and WebM all work. **PyAV is pinned to 18.1.0** — PyAV 19 removed an argument faster-whisper 1.2.1
still passes, and with the version pip picks by default *every* file fails with a `TypeError`.

## Output

```
Hello Jarvis. Please check the nightly build, and tell me if anything failed.
[stt: model=large-v3-turbo lang=en p=1.00 dur=6s wall=51s load=37s words=13]
```

(The test fixture, transcribed while other jobs kept the machine busy: 37 of the 51 seconds were loading
the model. On a quiet machine the load takes 3–8 s.)

| field | meaning |
|---|---|
| `lang=en p=1.00` | auto-detected, per 30-second window — a memo that switches language reads `lang=de+en` |
| `lang=en(forced)` | you passed `--language en` |
| `heard=de` | **forced language, but Whisper heard another one** — the text may be a *translation* (below) |
| `whisper=bg` | with `candidates`, what Whisper would have picked before the restriction overruled it |
| `words=0` | no speech heard (silence, noise) |
| `dur` / `wall` / `load` | memo length / total time / of which loading the model |

Failures end with **one** `stt: error: <reason>` line and a non-zero exit: **2** bad arguments or missing
file, **3** not decodable audio, **4** model or venv missing or broken, **5** transcription failed or
crashed, **6** longer than `max_minutes` (refused *before* the model loads, so a dispatcher waiting on it
never times out).

## Which model, and why (benchmark)

Rule: *the most accurate model that transcribes a 60-second memo in under 90 seconds* on a 6-core desktop
CPU with no GPU. Fresh process per run — exactly what the dispatcher pays. WER = word error rate against
the text the clip was made from.

| model (disk) | 59 s English memo | WER | notes |
|---|---|---|---|
| small (0.5 GB) | 10–12 s | 0.6 % | fast, weaker on accents and other languages |
| medium (1.5 GB) | 27–31 s | 0.6 % | |
| **large-v3-turbo (1.6 GB)** | **25–34 s** | **0 %** | **default** — best on accented English and on mixed-language memos |
| large-v3 (2.9 GB) | 123 s | 2 % | loading alone took 62–67 s: fails the rule |

A memo costs roughly 0.4–0.7× its own length plus the model load: 3–8 s on a quiet machine, **up to 90 s
while other jobs are running** (it is the machine being busy, not the disk — every model more accurate
than `small` has a 1.5–3 GB file, so no choice fixes it). Threads default to the physical core count:
on a 6-core/12-thread CPU, 12 threads and 4 threads were both slower than 6.

## Findings that set the decoder (each would have shipped a bug)

1. **A forced language silently translates.** Force English on a German memo and you get fluent English —
   with no sign it was ever German. So even in forced mode every window is asked which language it is in
   (one decoder step on an encoder output computed anyway, no measurable cost), and `heard=` says so.
   The dispatcher must mention it in its echo.
2. **Whisper does not know every language.** Unrestricted, it heard Macedonian as Bulgarian or Croatian —
   in the wrong script, word error rate 100 %. `candidates = ["mk", "en"]` restricts every language
   decision to the languages you actually speak (renormalised), which brought it down to ~15–30 %.
3. **One language decision per file loses the other language.** On a memo that switched languages, the
   second language came back translated on one run and *missing altogether* on the next. Now the decision
   is made per 30-second window, and conditioning on the previous window's text is off (across a switch it
   produced word salad).
4. **No glossary prompt, no hotwords.** A list of project names as `initial_prompt` or `hotwords` fixed some
   spellings — and on one long clip in five it **appended the glossary's own words as a sentence nobody
   said**, i.e. an instruction. The dispatcher acts on a transcript immediately, so an invented task costs
   far more than a misspelt name it can read for meaning. Neither is used.

## What the echo will look like

Names and numbers come out loose (`PR seven oh seven`, `per 707`, `get hub` for "GitHub"); with an
accent a long number can gain or drop a digit. The dispatcher reads for meaning, checks every number
against what exists, and asks one question when one resolves to nothing. Whisper can also invent a stock
closing line ("Thank you.") on trailing noise — the verbatim echo is how you catch it.

## Files

```
transcribe.py      decode (PyAV) -> language-restricted Whisper -> text + the [stt: ...] line
fetch_model.py     download one model, the default pinned by commit + sha256 (the only network step)
requirements.txt   faster-whisper 1.2.1, ctranslate2 4.8.2, av 18.1.0
../jarvis/stt.py   `python -m jarvis stt` / `stt-setup`
../tests/test_stt.py and tests/fixtures/hello.ogg (a synthetic voice, 6 s)
```
