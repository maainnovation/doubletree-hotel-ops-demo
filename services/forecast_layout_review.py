"""Known-layout normalization for HUMAN REVIEW, never native auto-admission.

Only explicitly enrolled daily-table tenants may use these alternatives. Source
bytes remain authoritative: no fuzzy labels, repaired digits, inferred dates,
hotel selection, or missing cells. The ordinary strict validator is unchanged.
"""
from datetime import datetime, timedelta
import re

from services.whatsapp_forecast import ForecastNeedsCorrection, SUPPORTED_METRIC_LABELS


ALIASES = {
    'arrivals': ('Arrivals',),
    'departures': ('Departures',),
    'stayovers': ('Stayovers',),
    'rooms_on_books': ('Rooms on books', 'Rooms on the books'),
    'forecast_pickup': ('Forecast pickup', 'Forecasted Pick up'),
    'forecast_occupied': ('Forecast occupied', 'Forecasted occupied rooms', 'Forcasted occupied rooms'),
    'occupancy_pct': ('Occupancy %',),
}
_TITLES = frozenset({'SYNTHETIC TEST ONLY - fictional hotel, counts and staff',
                     'Synthetic forecast - fictional data only'})
_WEEKDAYS = r'FRI\s+SAT\s+SUN\s+MON\s+TUE\s+WED\s+THU'


def normalize_known_review_layout(text, hotel_code, hotel_name, report_profile):
    """Return a canonical transcription plus adapter name, or fail closed.

This does not authorize calculation. Callers must validate the full matrix and
require a durable human confirmation even when the original is a native PDF.
"""
    if report_profile != 'hotel_daily_table_v1' or not isinstance(text, str) or len(text) > 100_000:
        raise ForecastNeedsCorrection('This alternate layout is not enabled for the enrolled report profile.')
    matches = []
    for field, aliases in ALIASES.items():
        labels = '|'.join(r'\s+'.join(re.escape(part) for part in label.split()) for label in aliases)
        found = list(re.finditer(r'^[ \t]*(?:' + labels + r')[ \t]*:?[ \t]*(?=\r?$|[0-9])', text, re.I | re.M))
        if len(found) != 1:
            raise ForecastNeedsCorrection('The alternate layout needs exactly one known row per metric; duplicate or unknown labels are not guessed.')
        matches.append((found[0].start(), found[0].end(), field))
    matches.sort()
    prefix = text[:matches[0][0]]
    headers, remaining = {}, []
    for line in (line.strip() for line in prefix.splitlines() if line.strip()):
        identifier = re.fullmatch(r'(?:Hotel|Property)[ \t]+(code|id|name)[ \t]*:[ \t]*(.+)', line, re.I)
        if identifier:
            kind = 'name' if identifier.group(1).casefold() == 'name' else 'code'
            value = identifier.group(2).strip()
        elif hotel_name is not None and line == hotel_name:
            kind, value = 'name', line
        elif line in _TITLES:
            if 'title' in headers:
                raise ForecastNeedsCorrection('The report contains duplicate headings.')
            headers['title'] = line
            continue
        else:
            remaining.append(line)
            continue
        expected = hotel_name if kind == 'name' else hotel_code
        if kind in headers or expected is None or value != expected:
            raise ForecastNeedsCorrection('The report has a conflicting or duplicate hotel identifier; no hotel mapping was guessed.')
        headers[kind] = value
    header = '\n'.join(remaining).strip()
    weekdays = re.match(_WEEKDAYS + r'(?=\s|$)', header, re.I)
    if weekdays:
        header = header[weekdays.end():].strip()
    elif headers.get('code') != hotel_code:
        raise ForecastNeedsCorrection('An alternate layout without the enrolled weekday header requires the exact hotel code.')
    header = re.sub(r'^Dates[ \t]*:[ \t]*', '', header, count=1, flags=re.I)
    header = re.sub(r'\s+Prior\s+Day\s+Occupancy\s*$', '', header, count=1, flags=re.I)
    iso = r'20[0-9]{2}-[0-9]{2}-[0-9]{2}'
    numeric = r'(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12][0-9]|3[01])/20[0-9]{2}'
    if re.fullmatch(iso + r'(?:\s+' + iso + r'){6}', header):
        fmt = '%Y-%m-%d'
    elif re.fullmatch(numeric + r'(?:\s+' + numeric + r'){6}', header):
        # Without an explicit date convention, all-low numeric dates are
        # ambiguous. Weekday arithmetic is not permission to invent one.
        if not any(int(value.split('/')[1]) > 12 for value in header.split()):
            raise ForecastNeedsCorrection('Numeric dates are ambiguous; resend explicit ISO dates or an unambiguous full-date report.')
        fmt = '%m/%d/%Y'
    else:
        raise ForecastNeedsCorrection('The alternate layout needs seven complete dates and no extra header text.')
    try:
        dates = [datetime.strptime(value, fmt).date() for value in header.split()]
    except ValueError:
        raise ForecastNeedsCorrection('The report contains an invalid full date.') from None
    if dates[0].weekday() != 4 or any(day != dates[0] + timedelta(days=i) for i, day in enumerate(dates)):
        raise ForecastNeedsCorrection('Printed dates must be one consecutive Friday-Thursday week; columns are not reordered.')
    values = {}
    for i, (_, end, field) in enumerate(matches):
        segment = text[end:matches[i+1][0] if i+1 < len(matches) else len(text)].strip()
        number = r'(?:0|[1-9][0-9]{0,5})' + (r'(?:\.[0-9]{1,2})?%?' if field == 'occupancy_pct' else '')
        if not re.fullmatch(number + r'(?:\s+' + number + r'){6}', segment):
            raise ForecastNeedsCorrection('Every known metric needs exactly seven explicit nonnegative cells, with no extra rows or text.')
        values[field] = segment.split()
    normalized = ['Hotel code: ' + hotel_code, 'Dates: ' + ' '.join(day.isoformat() for day in dates)]
    normalized += [label + ': ' + ' '.join(values[field]) for field, label in SUPPORTED_METRIC_LABELS.items()]
    return '\n'.join(normalized), 'known_rows_review_v1'
