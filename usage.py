"""Published credit estimates, separate from account-specific invoiced charges."""

MODEL = 'gpt-6.1-sol'
EFFORT = 'xhigh'
SERVICE_TIER = 'fast'
PRICING = {
    'as_of': '2026-10-26',
    'source': 'https://learn.chatgpt.com/docs/pricing',
    'basis': 'reference purchased-credit / Enterprise pay-as-you-go rates; not an invoice',
    'input_per_million': 50,
    'cached_input_per_million': 2.5,
    'output_per_million': 250,
    'fast_multiplier': 2,
}


def valid_usage(value):
    if not isinstance(value, dict):
        return False
    keys = ('input_tokens', 'cached_input_tokens', 'output_tokens')
    if any(type(value.get(key)) is not int or value[key] < 0 for key in keys):
        return False
    return (value['cached_input_tokens'] <= value['input_tokens']
            and type(value.get('reasoning_output_tokens', 0)) is int
            and 0 <= value.get('reasoning_output_tokens', 0) <= value['output_tokens'])


def estimate_credits(value):
    if not valid_usage(value):
        return None
    # Cached tokens are part of input, and reasoning tokens are part of output.
    return round(PRICING['fast_multiplier'] * (
        (value['input_tokens'] - value['cached_input_tokens']) * PRICING['input_per_million']
        + value['cached_input_tokens'] * PRICING['cached_input_per_million']
        + value['output_tokens'] * PRICING['output_per_million']) / 1_000_000, 8)


def reported_usage(events):
    turns = [event.get('usage') for event in events if event.get('type') == 'turn.completed']
    if not turns or any(not valid_usage(turn) for turn in turns):
        return None
    return {key: sum(turn.get(key, 0) for turn in turns) for key in
            ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens')}
