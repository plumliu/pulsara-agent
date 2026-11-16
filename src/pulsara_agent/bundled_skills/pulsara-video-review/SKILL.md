---
name: pulsara-video-review
description: Review local video, inspect continuous actions and spatial details, read timed transcripts, and annotate findings. Use for sports, nature, experiments, films, lectures and screen recordings, including 视频审看、动作回看 and 字幕理解. Also supports audio-only speech review.
---

# Review local video

Keep the video's PATH Target and use seconds on its source timeline, including
decimals. Media tools are available directly in the main conversation and through
Code Mode. For script arguments and complete return types, read the relevant
[Code Mode media API pages](../pulsara-code-mode/references/tool-calling/api/index.md#media).

Use FFmpeg/ffprobe through terminal tools when duration, dimensions or track
information helps choose the read. The first actual video read opens it in the
sidebar. Use open_in_pulsara for a separate report or other artifact; the user can
play and browse the video independently while you work.

## Choose the evidence needed

| Question | Read |
| --- | --- |
| What is broadly present, or where is a relevant passage? | read_video_frames with action:"overview": sparse representative images across the range |
| What is visible at this location: text, a number, an object or a posture? | action:"detail": clear local state observations |
| What happens between states, in what order, for how long or how many times? | action:"motion": all actual source presentation observations in time order |
| What was said? | read_transcript; use its reported subtitle/ASR source |

Start with the action that answers the question. A whole-video overview can give
a useful general account or candidate locations. A known-time state question can
start with detail; a known-time process question can start with motion. Continue
with the unresolved question, and answer when the delivered evidence is sufficient.

An overview can be refined with local overviews. Its representative intervals
help navigation; they do not establish what happened at every intervening moment.
For first occurrence, exact count or absence throughout an interval, obtain the
process evidence needed across that interval. A clear observed example can settle
an existence question, while order and repetition require the relevant sequence.

## Look closely, then follow the process

Detail requires explicit from/to with 0 < to-from <= 2 seconds. It targets the
start, midpoint and just before the end, yielding at most three distinct source
observations. Several targets may resolve to one frame. Use actual returned t
values to locate what you saw.

Use crop:[left,top,right,bottom] for the displayed frame's normalized corners.
For example, [0.6,0.1,0.9,0.8] selects 60–90% across and 10–80% down. The same
crop applies throughout a request. For a contact or movement question, retain
the relevant body parts, objects and reference surface. Compare distant states
with separate local detail reads and comparable crops.

For a long-jump review, overview can locate approach, flight and landing. Motion
around landing provides the sequence. If contact is hard to see, detail with a
suitable crop can clarify the hand, heel and sand. Return to motion when their
order remains unresolved. If the original motion images already answer it,
proceed to the answer. A question about earlier loss of balance may require an
earlier motion range.

Motion is also appropriate for moving cameras, occlusion, repeated movements and
brief changes. It preserves source observations; read the pictures and time
index to interpret them. Detail's clear sampled states can support that work but
do not supply the missing process between its targets.

## Choose the range and read the result

Overview and motion default to the full video when endpoints are omitted. Use
the relevant range when it is known; whole-interval or whole-video motion is
appropriate when the question requires it. The tool completes and saves the
request before returning, so a wider motion range incurs its source-processing
cost even if you later stop reading pages.

Overview and motion return each collage's frames index followed by its image.
Detail pairs each observation's time index with its clear image. For collages,
read n, t, cell_rect and applicable time intervals; detail provides t,
visible_range and applicable dt. Read those indices together with the pictures.
A source frame can start before from while still covering the requested start. Reported
track gaps or unreadable details qualify the conclusions you can draw.

Native results use frozen 12k/NEXT pages. Pass the returned NEXT Target as
read_file's target when remaining evidence is needed. It reads the same saved
result; a new time range or crop starts a new request. Stop when the question is
answered. Code Mode returns the complete saved value; explicit output delivers
the selected text and images to the model. Reopen a delivered IMAGE_REF with
view_image when useful.

## Review with the user

Use annotate_media to leave useful findings on the original video's timeline:
an observed stage, an action and result, or a passage to revisit. Set from/to
to the finding's interval and write concise text grounded in the observed
pictures or transcript. A note can span several reads.

When the user asks about a passage, switch to the overview, detail or motion
that resolves it, then resume the wider review as appropriate. Ordinary playback
and scrubbing remain independent of your task. For speech questions, combine
read_transcript with visual reads as needed; read the reported failure and any
obtained segments when transcription is incomplete.

Link the reviewed video naturally in the answer, for example
`[trial landing](/absolute/path/to/video)`, and anchor findings to actual times.
The [worked examples](references/worked-examples.md) show continuous-motion tasks
and different entry points.
