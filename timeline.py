"""Presentation helpers for dated observations."""
from collections import defaultdict


def date_variants(events):
    dates = defaultdict(set)
    for event in events:
        dates[event["title"].casefold()].add(event["date"])
    return {title for title, options in dates.items() if len(options) > 1}


def event_label(event, variants):
    labels = [event["date"], event["title"]]
    labels.append("Есть источник" if event["evidence"] is not None else "Гипотеза")
    if event["title"].casefold() in variants:
        labels.append("Одно название, разные даты")
    return " · ".join(labels)
