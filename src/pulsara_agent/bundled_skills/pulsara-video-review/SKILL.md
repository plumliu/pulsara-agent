---
name: pulsara-video-review
description: Review videos progressively, inspect actions and screen details, use timed subtitles or speech transcripts, and record evidence-based findings. Use for 视频审看、动作回看、录屏定位、字幕理解 with the main agent's direct media tools. Also supports speech review from audio-only files.
---

# Review local video

Use the direct media tools in the main conversation. Keep the original PATH target and use seconds on the source timeline.

## Choose a starting point

- **Summarize a whole recording.** For video, a target-only `read_video_frames` gives a sparse overview. Use the time index to sketch the likely topics. Then work through the recording in order, checking what each passage adds and revising the topic boundaries as you go. A brief event can fall between overview frames.
- **Find an error or verify an action.** If the user gives a timestamp or you already have a candidate passage, start there. For a spoken clue, read the relevant transcript. For a fleeting action, inspect the nearby visual sequence. Widen the search if the clue does not hold up.
- **Understand speech.** Go straight to `read_transcript` for audio-only material or a question about what was said. Identify whether the words came from embedded subtitles or ASR. For burned-in captions, slide text or UI labels, read the picture.

Choose evidence within each window by the claim you need to establish. Use the transcript for spoken arguments and explanations; inspect the corresponding frames for charts, demonstrations and screen results. Reading every subtitle does not establish what happened visually. A visual-only question need not trigger transcription, and a window need not use every tool.

## Decide after each window

Choose a `from` and `to` that can answer one question. Follow a continuous explanation or a static slide over a longer span. Shorten the window where screens change rapidly or the order of actions matters. The 180-second limit applies to newly transcribed audio per call. Choose reading windows by content.

Read the returned evidence before choosing the next call. Check the actual timestamps and `processed_range`. What did this passage establish? What is still missing for the user's answer?

- **More of the same request.** If the missing evidence could be in the undelivered portion, copy the entire returned `next` call, including its `continuation` and original range. Frame pages first prioritize coverage across the request; each page is ordered by time, but the next page can return to an earlier time. Frame numbers are local to the page. A partial overview is not a completed map. A text page can end mid-sentence even when the audio has already been processed, and an empty text page can advance in time. To quote or save a text page already delivered, use its SESSION_ENTRY reference. To revisit a saved image, use its IMAGE_REF with `view_image`.
- **Another passage.** Start a fresh request with explicit `from` and `to`. For a whole-recording summary, carry unfinished topics across windows and account for the ending. For a targeted question, jump to the next plausible location.
- **Unclear evidence.** Use `motion` over a short interval to see the sequence before, during and after a change. The first sampled frame showing a change proves it was visible by that time. If the preceding sample is several seconds earlier, give that interval or look closer before claiming an exact onset or a delay relative to subtitles. Use `detail` with a normalized crop to read small text. To compare distant states, inspect each time separately with the same crop. A spoken prediction and a later screen state support different claims.
- **Enough evidence.** Answer once the requested scope is covered, even if an optional `next` remains. A null `next` means the tool offers no continuation. Check `complete`, the processed range and any errors before claiming coverage. If an upload outcome is unknown, explain the gap and possible charge before considering a fresh request.

The sidebar shows the model's latest reading range. Let the user scrub and preview independently. Follow an explicit steer when the user changes the question or asks you to inspect a different time. Use `annotate_media` for a finding worth leaving on the timeline. Give its source interval and the observation that supports it. A topic can span several windows before a note is useful; continue reading quietly while it develops.

## When the next step is unclear

A subtitle saying “I'm saving the report now” establishes intent. To verify the outcome, inspect the frames just after the action. If the clip ends on the click, say that the outcome is unconfirmed.

For a long recording reviewed across windows, two readings of the same short tutorial, or a transcript that breaks mid-sentence, see [Worked examples](references/worked-examples.md). Use the example that matches the decision in front of you.
