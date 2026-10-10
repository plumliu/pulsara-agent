"""Deterministic Unicode excerpts with whole-message annotation barriers."""

from __future__ import annotations
from dataclasses import dataclass
from pulsara_agent.llm.estimator import PulsaraHeuristicTokenEstimatorV3

ESTIMATOR = PulsaraHeuristicTokenEstimatorV3()
HISTORY_BODY_TOKENS = 1024  # Per-candidate product budget, never corpus/lifetime.


@dataclass(frozen=True, slots=True)
class HistoryCandidate:
    session_id: str
    q1_entry_id: str
    title: str
    score: float
    document: dict
    messages: tuple[dict, ...]

    def projection(self):
        return {
            "source": "session_history",
            "title": self.title,
            "messages": [
                {k: m[k] for k in ("role", "label", "at", "text")}
                for m in self.messages
            ],
            "historical_advisory": True,
        }


def earliest_hit(document, positions):
    """Map only actual engine hits; separators and quote have no field mapping."""
    hits = []
    raw = document["body"].encode("utf-8")
    for field_positions in positions:
        for pair in field_positions or ():
            start, end = pair
            for span in document["positions"]:
                if span["start"] <= start < end <= span["end"]:
                    try:
                        offset = len(raw[span["start"] : start].decode("utf-8"))
                        raw[start:end].decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                    hits.append(
                        (start, span["message"], span["display_start"] + offset)
                    )
    if not hits:
        return None
    _, message, offset = min(hits)
    return message, offset


def _weights(message):
    # The public estimator is linear in weighted UTF-8 bytes, then rounds once.
    # Count canonical body only: annotation identity labels are packaging.
    weights = [0] * len(message["text"])
    for start, end in message.get("body_ranges", [[0, len(weights)]]):
        for at in range(start, end):
            weights[at] = ESTIMATOR.estimate_text(message["text"][at] * 4)
    return weights


class _Region:
    def __init__(self, messages, center=None, mode="center"):
        self.messages = messages
        self.text = "\n".join(m["text"] for _, m in messages)
        self.spans = []
        self.weights = []
        offset = 0
        for i, m in messages:
            self.spans.append((offset, offset + len(m["text"]), i, m))
            if self.weights:
                self.weights.append(0)  # Message boundary is packaging.
            self.weights.extend(_weights(m))
            offset += len(m["text"]) + 1
        self.selected = set()
        self.units = 0
        self.closed = [False, False]
        if mode == "ends":
            self.cursors = [0, len(self.text) - 1]
            self.steps = [1, -1]
        elif mode == "head":
            self.cursors, self.steps = [0, 0], [1, 1]
            self.closed[1] = True
        elif mode == "tail":
            self.cursors = [len(self.text) - 1] * 2
            self.steps = [-1, -1]
            self.closed[1] = True
        else:
            self.cursors, self.steps = [center - 1, center], [-1, 1]

    def add(self, additions):
        additions = set(additions) - self.selected
        self.units += sum(self.weights[x] for x in additions)
        self.selected.update(additions)

    def grow(self, soft, controller):
        initial = self.units
        spent = [0, 0]
        while self.units - initial < soft * 4 and not all(self.closed):
            available = [i for i in (0, 1) if not self.closed[i]]
            side = min(available, key=lambda i: (spent[i], i))
            at = self.cursors[side]
            if not 0 <= at < len(self.text):
                self.closed[side] = True
                continue
            span = next((s for s in self.spans if s[0] <= at < s[1]), None)
            if at in self.selected:
                if span is not None and span[3]["annotated"]:
                    # reachable() may have protected this whole message before
                    # growth starts. Resume outside it, without charging twice.
                    self.cursors[side] = (
                        span[1] if self.steps[side] > 0 else span[0] - 1
                    )
                else:
                    self.closed[side] = True
                continue
            if span is not None and not controller.reachable(span[2]):
                self.closed[side] = True
                continue
            if span is not None and span[3]["annotated"]:
                # reachable() retained the entire annotation, possibly over soft quota.
                self.cursors[side] = span[1] if self.steps[side] > 0 else span[0] - 1
                spent[side] += self.units - initial - sum(spent)
                continue
            cost = self.weights[at]
            if controller.units + cost > controller.budget * 4:
                self.closed[side] = True
                continue
            if self.units - initial + cost > soft * 4:
                # Soft exhaustion is reopenable during budget redistribution.
                break
            self.add([at])
            spent[side] += cost
            self.cursors[side] = at + self.steps[side]

    def render(self):
        output = []
        for start, end, _, message in self.spans:
            selected = sorted(x for x in self.selected if start <= x < end)
            if not selected:
                continue
            ranges = []
            left = right = selected[0]
            for x in selected[1:]:
                if x == right + 1:
                    right = x
                else:
                    ranges.append((left, right + 1))
                    left = right = x
            ranges.append((left, right + 1))
            text = "\n[truncated]\n".join(self.text[a:b] for a, b in ranges)
            if ranges[0][0] > start:
                text = "[truncated]\n" + text
            if ranges[-1][1] < end:
                text += "\n[truncated]"
            output.append((message["sequence"], {**message, "text": text}))
        return output


class _Controller:
    def __init__(self, regions, messages, center, budget):
        self.regions, self.messages = regions, messages
        self.center, self.budget = center, budget
        self.barriers = [None, None]
        self.forced = False
        self.stopped = False

    @property
    def units(self):
        return sum(r.units for r in self.regions.values())

    def reachable(self, target):
        if self.stopped:
            return False
        side = 0 if target < self.center else 1
        barrier = self.barriers[side]
        if barrier is not None and (
            target <= barrier if side == 0 else target >= barrier
        ):
            return False
        path = (
            range(self.center, target - 1, -1)
            if side == 0
            else range(self.center, target + 1)
        )
        for index in path:
            message = self.messages[index]
            if not message["annotated"]:
                continue
            region = self.regions[message["region"]]
            span = next(s for s in region.spans if s[2] == index)
            additions = set(range(span[0], span[1])) - region.selected
            if not additions:
                continue
            if self.forced:
                self.stopped = True
                return False
            if self.units + sum(region.weights[x] for x in additions) > self.budget * 4:
                self.barriers[side] = index
                self.forced = True
                return False
            region.add(additions)
        return True


def excerpt_history(document, positions, *, budget=HISTORY_BODY_TOKENS):
    messages = document["messages"]
    if (sum(sum(_weights(m)) for m in messages) + 3) // 4 <= budget:
        return tuple(messages)
    hit = earliest_hit(document, positions)
    if hit is None:
        return None
    hit_index, offset = hit
    hit_message = messages[hit_index]
    if hit_message["annotated"] and (sum(_weights(hit_message)) + 3) // 4 > budget:
        return None
    hit_region = hit_message["region"]
    grouped = {
        name: [(i, m) for i, m in enumerate(messages) if m["region"] == name]
        for name in ("Q1", "A", "Q2")
    }
    regions = {}
    for name, values in grouped.items():
        if not values:
            continue
        center = 0
        if name == hit_region:
            for i, m in values:
                if i == hit_index:
                    center += offset
                    break
                center += len(m["text"]) + 1
            mode = "center"
        elif name == "A":
            mode = "head" if hit_region == "Q1" else "tail"
        else:
            mode = "ends"
        regions[name] = _Region(values, center, mode)
    order = list(dict.fromkeys((hit_region, "Q1", "Q2", "A")))
    controller = _Controller(regions, messages, hit_index, budget)
    # Protect the center annotation before distributing ordinary text quotas.
    if not controller.reachable(hit_index):
        return None
    for name in order:
        if name in regions:
            quota = 512 if name == hit_region else 256
            remaining = max(0, budget - (controller.units + 3) // 4)
            initial_need = max(0, quota - (regions[name].units + 3) // 4)
            regions[name].grow(min(initial_need, remaining), controller)
    for name in order:
        if name in regions:
            remaining = max(0, budget - (controller.units + 3) // 4)
            if remaining:
                regions[name].grow(remaining, controller)
    result = tuple(
        m for _, m in sorted(x for r in regions.values() for x in r.render())
    )
    # Markers consume provider packaging budget, not retained-body quota.
    if not result:
        return None
    # A whole omitted message is still a gap. Mark it on a retained neighbor;
    # never create an empty historical message or split an atomic annotation.
    retained = {m["entry_id"] for m in result}
    marked = [dict(m) for m in result]
    previous = -1
    for index, message in enumerate(marked):
        original = next(i for i, m in enumerate(messages) if m["entry_id"] == message["entry_id"])
        if any(m["entry_id"] not in retained for m in messages[previous + 1:original]):
            message["text"] = "[truncated]\n" + message["text"]
        previous = original
    if previous < len(messages) - 1:
        marked[-1]["text"] += "\n[truncated]"
    return tuple(marked)
