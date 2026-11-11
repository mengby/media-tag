"""Separate missing cleaning choices from actual processing failures."""


class CleaningError(ValueError):
    state = 'failed'

    def __init__(self, message, *, code='cleaning_failed'):
        super().__init__(message)
        self.code = code


class CleaningPending(CleaningError):
    state = 'waiting_policy'

    def __init__(self, message, *, code, state='waiting_policy'):
        super().__init__(message, code=code)
        if state not in ('waiting_policy', 'waiting_evidence'):
            raise ValueError('invalid cleaning pending state')
        self.state = state
