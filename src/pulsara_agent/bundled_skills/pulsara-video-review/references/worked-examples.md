# Worked video-review examples

Times and crops below illustrate decisions; use the positions found in the
actual video. Each next read follows what is still unresolved.

## Summarize a long jump and check contact order

The user asks for an account of a trial and whether the hand or heel touches the
sand first.

1. Use overview across the trial to locate approach, take-off, flight and landing.
2. Read motion around landing. For example, if the candidate is around 42 s:

```json
{"target":{"kind":"PATH","path":"trial.mp4"},"from":41.6,"to":43.2,"action":"motion"}
```

3. If contact is unclear, use detail around a returned frame time. A crop can
   retain the hand, heel and sand while making their positions easier to see:

```json
{"target":{"kind":"PATH","path":"trial.mp4"},"from":42.3,"to":42.38,"action":"detail","crop":[0.2,0.45,0.8,1]}
```

4. If order is still unresolved, read motion over the relevant contact interval
   with that suitable crop. The clear detail posture helps identify what to
   compare; the neighboring observations support order.
5. If the user asks when balance was lost, inspect the preceding flight with
   motion. Distinguish visible sequence and posture from a causal explanation
   that would need further evidence.

If the first motion already shows the answer, finish there. Add a timeline note
when it would help the user revisit the landing, and link the video in the answer.

## Start directly with a bird leaving a branch

The user points to 18 s and asks whether the bird spreads its wings before its
feet leave the branch. Start with motion around that time. A preceding overview
is unnecessary when the location is already known.

If the feet are difficult to identify, use detail near a returned time and a
crop retaining the bird and branch. Return to motion over the unresolved
transition if needed. Use the actual times and visibility; a sampled pose or
occluded foot alone does not settle the order.

## Follow a liquid-drop experiment

The user asks whether recoil precedes breakup after maximum spreading.
Overview can locate the experiment and its broad stages. Motion follows impact,
spreading and the subsequent sequence. Detail can identify a rim filament or
read a scale; return to motion to establish when it appears relative to breakup.

Once the maximum-spreading moment is established, a question about its diameter
may end with a clear detail read and scale. A question about which event happens
first needs the relevant sequence.
The crop should retain the reference surface and the parts being compared.

## Read a known-time state or compare distant states

“What amount is visible at 12.4 s?” starts with detail around that time, such as
12.32–12.56 s. Crop the display if its position is known, and use the returned
frame times when reporting the amount.

“How does the result at 30 s compare with 90 s?” can use separate local detail
reads with comparable crops. Inspect the intervening process only when it is
part of the question. Reuse a delivered picture when it already answers it.

## Review a longer passage and verify repetition

For a general account, use a whole-range overview and refine relevant passages
with local overviews. Read a state with detail or a process with motion when
that part of the account needs it. A useful broad summary need not call all
three actions.

For “How many rotations occurred?” or “Was this the first contact?”, sparse
candidates are a starting point. Read the relevant process across the interval
needed for the count or first-occurrence claim. A full-range motion request can
be appropriate. Keep track of the actual evidence already delivered when
continuing through its saved pages.

If a clear picture establishes that an object appeared, that may settle an
existence question. An absence claim across a range needs adequate coverage and
visibility throughout that range.

## Continue saved evidence or change the question

A native response with NEXT has more saved content. Pass that NEXT Target to
read_file when it contains evidence still needed. The remaining content was
already generated; reading it does not decode the source again.

A different time range or crop is a new read_video_frames request. Stopping NEXT
can save further model reading, while the source-processing cost of the completed
request has already been incurred. Code Mode instead returns the complete value;
Use console.log to send the chosen content to the model.

The same frozen-result rule applies to read_transcript. If a delivered segment
ends before the needed explanation, continue the saved result. Use its reported
source and actual segment times, and distinguish obtained text from an incomplete
recognition result.

## Annotate and respond during a shared review

Once an observation is clear, use annotate_media with the original PATH Target,
the finding's from/to and concise text. For a landing, the note may identify the
observed contact order and useful return point. For an experiment, it may mark
maximum spreading or the visible onset of breakup.

When the user asks about a note, inspect that passage with the appropriate
action. Add a further finding if useful, then continue the wider task. Video
playback remains under the user's control throughout the review.
