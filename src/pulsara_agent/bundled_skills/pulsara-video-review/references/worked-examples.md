# Worked examples

## A long recording across reading windows

Suppose the user asks for a summary of a 12-minute dashboard tutorial, including how its metrics are calculated. The times below illustrate content-based choices, not fixed window sizes.

1. Use a target-only `read_video_frames` overview to locate likely setup, chart and export sections. Treat these as tentative boundaries; the sparse images do not establish the full procedure or calculation.
2. Read the explanation in 0–120 s with `read_transcript`, and inspect the relevant setup frames. If the transcript returns `next`, use it as needed to finish this window before moving on. Suppose this passage describes the inputs but leaves the date filter unexplained. Keep that question open rather than announcing a complete calculation or adding a premature note.
3. Start a new 120–300 s window. Read the explanation and inspect the chart where its interpretation matters. Suppose the presenter now says the calculation includes only the selected month. Revise the earlier tentative understanding: the metric is not an all-time total. If the date label is too small in the sampled pictures, use `detail` with a crop around 245–250 s to check the displayed filter. Use a short `motion` read only if how the filter changed is itself relevant.
4. Once the explanation and visible filter support the finding, add an `annotate_media` note spanning the relevant passages. Resume the main review at 300 s, not at the end of the short detail or motion revisit. A narrower check did not deliver the remaining tutorial.
5. Continue through the remaining topics and the ending at 720 s, adjusting each window to the content. A static explanation may need fewer images; a new screen result may need closer inspection. Summarize the supported procedure and calculations with source times, and state any visual or textual gaps that remain.

Each window changes the working understanding. A topic may cross windows before it is ready to summarize; neither a chapter guess nor one tool's completion means the whole recording has been reviewed.

## One tutorial, two questions

A 90-second screen recording shows a sales report being prepared. The following readings use the same clip. The times belong to this recording.

**“Summarize the tutorial. Did the final save work?”**

1. Read the sparse overview to locate the main screens. The last screen mentions a read-only folder. That gives a place to investigate, but the thumbnails alone leave gaps in the walkthrough.
2. Read the embedded subtitles. They describe importing a sales CSV, grouping it by region, then saving to a shared folder. The last cue says “I'm saving the report now.” That accounts for the procedure. The outcome still needs visual evidence.
3. Inspect 83–90 s with `read_video_frames` in `motion` mode. The 84 s frame shows the save dialog. At 85 s the screen reads “Save failed: read-only folder.” The text is legible, so the review can stop here.
4. Answer with the three steps and the failed save. A useful `annotate_media` note at 85–90 s is “The app reports ‘Save failed: read-only folder.’” There is no need to interrupt each earlier step with a note.

**“Just check the last ten seconds. When can you first confirm failure?”**

Start at 80–90 s. Inspect the ordered frames and, if the question asks what the presenter said, read the overlapping subtitle. If the change is hard to place, narrow the visual window around 84–85 s. Use `detail` with a crop if the lettering is hard to read.

A precise answer is “The dialog is still visible at 84 s. The first frame showing the failure is at 85 s. The subtitle says the presenter is saving.” This answers the timing question without reconstructing the preceding minute. If the only evidence available were thumbnails at 80 and 86 s, report that the message was visible by 86 s or inspect the intervening frames.

## A transcript page ends mid-explanation

Suppose a lecture summary needs the reasoning behind a design choice. In the requested 240–360 s window, the last delivered words are “We rejected the second design because …”. The tool reports that it processed through 360 s and returns a `next` call.

- **To finish the explanation**, send the complete returned call, including its continuation. Starting a new request at 360 s would skip words on the next page of this window.
- **Once the reason is clear**, decide what the user's scope still requires. For a whole-lecture summary, follow the discussion through the remaining topics and conclusion. A fresh `from`/`to` request is appropriate when moving to a new passage. For a question solely about that design choice, answer from the timed explanation as soon as it is sufficient, even if another page is available.
- **If the page ends in an error**, inspect the actual coverage and the reported gap. An unknown upload outcome with `next: null` leaves the missing words unverified. Say where the evidence ends instead of completing the sentence from inference.
