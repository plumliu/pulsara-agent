---
name: pulsara-media-review
description: Review local video frames, embedded subtitles and speech transcripts, compare moments in a clip, and attach evidence-based time notes. Use for 视频审看、动作回看、录屏定位、字幕和音频转写 when the main agent has the direct media tools.
---

# Review a local clip

Choose the evidence that can answer the question. Call these tools directly from the main conversation with a PATH target.

- To orient yourself, call `read_video_frames` with just the target. Use the numbered images and their time index to find candidate passages. A sparse overview can miss a brief change.
- To read small text or compare two states, use `detail` around the relevant times. For separate moments, make separate calls. Keep the same crop when comparing. Crop coordinates are normalized on the displayed frame.
- For a sequence of actions, use `motion` with a narrower `from` and `to`. Use actual frame times and the gaps between them to explain what the pictures establish. When the process is ambiguous, narrow the range again or say which part remains unclear.
- For speech or subtitles, use `read_transcript` directly. Quote the returned source and its actual time. Embedded captions can differ from the spoken words or the picture. Read hard subtitles, slide text and UI labels in clear frames.
- Continue by copying the complete returned `next` call. An empty transcript page can still advance in time. If the service reports an unknown upload outcome, explain the potentially billable gap before considering a new request. To quote or save an already delivered page, use its SESSION_ENTRY reference.

For a recording of a software demonstration, for example, find the relevant screen in the overview, inspect the action with motion, then crop the confirmation message in detail. A caption predicting success and a visible confirmation are two separate observations.

Use `annotate_media` when you have an observation worth retaining. Supply the original target, a time interval in source seconds, and the note text. A note may span several reading windows. Cite the specific visual or textual evidence and preserve uncertainty. Continue through an unfinished passage without posting a note after each call. The user's explicit steer can redirect your review.
