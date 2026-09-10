import hashlib


def digest(parts):
    return hashlib.sha256('\0'.join(map(str, parts)).encode()).hexdigest()


class TrafficRequest:
    @classmethod
    def model_validate(cls, request):
        traffic = request.get('traffic') if isinstance(request, dict) else None
        invalid = not isinstance(traffic, list) or len(traffic) > 100
        if not invalid:
            for line in traffic:
                for side in ('source', 'destination'):
                    items = ((line.get(side) or {}).get('items') or [])
                    if any(set(item) != {'address'} for item in items):
                        invalid = True
        if invalid:
            raise ValueError('request rejected by test connector contract')
        return request


class FireFlow:
    def __init__(self, config, mode, audit, request=None):
        self.config = config
        self.mode = mode
        self.audit = audit
        self.request = request
        self.state = audit.directory

    def get(self, *_args, **_kwargs):
        raise OSError('FireFlow is unavailable in the public unit-test contract double')

    def create(self, *_args, **_kwargs):
        raise OSError('FireFlow is unavailable in the public unit-test contract double')
