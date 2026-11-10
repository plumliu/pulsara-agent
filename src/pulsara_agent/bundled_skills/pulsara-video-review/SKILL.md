---
name: pulsara-video-review
description: Review videos, read screen details, inspect actions and changes, use timed subtitles or speech transcripts, and leave findings on the timeline. Use for 视频审看、动作回看、录屏定位、字幕理解 with the main agent's direct media tools. Also supports speech review from audio-only files.
---

# Review local video

Use the direct media tools in the main conversation. Keep the original PATH target and use seconds on the source timeline, including decimal seconds.

Before reviewing a video, you can use the terminal tool with FFmpeg/ffprobe to check its duration, dimensions, and audio or subtitle tracks when that context helps choose what to inspect.

The first actual read of a video with `read_video_frames` automatically opens that video in the sidebar. This already shows the video to the user, so there is no need to call `open_in_pulsara` before reviewing or again when you finish. Use `open_in_pulsara` for a separate report or other new artifact that the user should see. The user controls playback and can keep browsing independently while you read.

## Choose the tool by the question

| What you need | Action |
| --- | --- |
| Find a topic, screen or likely event location | `read_video_frames` with `mode="overview"` over the relevant range |
| Read text or numbers; identify an object or local state | `mode="detail"` with explicit `from` and `to`, at most 2 seconds apart; add `crop` for small details |
| Examine an action, order, appearance/disappearance, repetition or A→B→A change | `mode="motion"` over the relevant sequence |
| Understand spoken arguments or explanations | `read_transcript`; check whether the text comes from subtitles or ASR |

For a whole-video summary, start with a target-only overview to get a map of the content. Use further overviews on the passages you need to explore, including ranges in the middle of the video. Follow the topics through to the ending, narrowing the range around dense material and carrying unfinished questions between passages. Inspect particular text or actions as they become relevant.

For a targeted question, begin at the supplied time or the best candidate location. “What is the amount at 30 seconds?” calls for detail. “What happened around 30 seconds?” calls for motion. “Which screen is this section about?” calls for a local overview. Choose the next action from what remains to be answered.

For audio-only material or a speech question, go straight to `read_transcript`. Use detail for burned-in captions, slide text and UI labels. Choose visual and spoken evidence independently according to the task.

## Read a local detail

Choose a range around the content you want to identify. Both `from` and `to` are required, with `0 < to - from <= 2` seconds. Detail has exactly three sampling positions—start, midpoint and just before the end—and returns at most three pictures. For example, `from=12.32, to=12.56` concentrates those positions in a short interval. The returned index gives their actual source-frame times. Several positions may resolve to one source frame.

Use a normalized `crop=[left,top,right,bottom]` to focus on a label, face, object or panel. These are the top-left and bottom-right corners: `[0.6,0.1,0.9,0.8]` selects the area from 60% to 90% across and 10% to 80% down the displayed frame. The same crop applies to each picture. Narrow the time range to concentrate on a brief state; tighten the crop to enlarge spatial detail. For a distant before/after comparison, inspect each location separately with a comparable crop.

If a detail request exceeds two seconds, use overview to find the relevant screen in the longer passage, or motion to examine its sequence. Once located, read a short detail around the item you need. Build detail calls around specific questions rather than covering the recording in consecutive two-second blocks.

## Inspect changes with motion

Use motion whenever the answer depends on what happened between states: a click followed by a dialog, a menu opening and closing, a repeated movement, or a brief error. Read the frames in time order and follow `next` when the needed part of the sequence remains to be delivered. For a broad search, overview can help locate a candidate passage before the close review.

If the user asks whether a brief event occurred anywhere within a specified interval, examine that interval with motion through the required range. When an event is located, use its actual timestamps in the answer. If lettering is too small, add detail around that frame with a crop. If the pictures already answer the question, proceed to the answer.

## Decide after reading

Naturally incorporate a Markdown link to the reviewed video into the final answer, such as `[brief video description](/absolute/path/to/video)`, even when no video was modified.

Read the actual pictures and time index, then decide what is still needed:

- **Answer is ready:** respond using the relevant times. Stop even if an optional `next` remains.
- **Needed evidence is on a remaining page:** copy the complete `next` call, retaining its original range and continuation. A detail next delivers the remaining selected pictures from the same short range.
- **Another passage matters:** start a fresh request at that location. After a local revisit, resume the main review where you left it.
- **Content is hard to identify:** use detail/crop. For order or a fleeting state, use motion. Report unreadable content or an unfinished interval when it affects the requested answer.

Overview pages prioritize coverage across the range, so a later page can return to an earlier time. Use the actual frame index to locate each picture. Anchor summary topics to the frames you saw; use motion to locate an exact transition time when the question needs it. The burned number identifies the image; several time observations can share it. `processed_range` describes preparation, while the frame index lists the pictures delivered. Copy `next` for continuation instead of calculating a new start from `processed_range`. Check `complete` and any source-range errors when a task requires finishing the requested range.

Reuse pictures already delivered when they answer the question. Reopen a saved image through its IMAGE_REF with `view_image`. To quote or save an already delivered text page, use its SESSION_ENTRY reference. Transcript pages can end mid-sentence; continue when the rest of the explanation is needed. If an ASR upload outcome is unknown, explain it and the possible repeat charge before making a fresh request.

## Review long videos with the user

Use `annotate_media` during a long review to build a shared timeline of useful findings: a section's main point, a key operation and its result, or an issue worth revisiting. Add a note when the finding becomes clear, so the user can inspect it while you continue through the video. Let the content determine when a note is useful.

Combine the frame modes around each finding: locate the passage with overview, including a narrow local overview; read relevant text or objects with detail; inspect the process with motion when sequence matters. Use the modes the question needs, then call `annotate_media` with the original target, the finding's `from/to`, and concise, readable `text`. The note's interval follows the finding and can span several reads. Write the takeaway and the concrete visual detail that makes it useful to the user.

The user can select a timeline note to jump back to that passage, ask about it, or redirect your review. Respond to an explicit question or steer with the appropriate local overview, detail or motion, add a further note when useful, and resume the main review unless the user changes the goal. Let ordinary scrubbing remain independent of your reading position. For a simple question at a known time, answer directly once the content is clear; add a note when it would help the user revisit the result.

For complete examples of these choices, see [Worked examples](references/worked-examples.md).
