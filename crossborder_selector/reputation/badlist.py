import requests
from crossborder_selector.models import SourceResult
from crossborder_selector.reputation.base import ReputationSource


def _http_get(url: str) -> str:
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.text


class BadListSource(ReputationSource):
    name = "badlist"

    def __init__(self, url: str, weight: float = 80.0, fetcher=None):
        self.url, self.weight = url, weight
        self._fetcher = fetcher or _http_get
        self._ips = None

    def _load(self):
        if self._ips is None:
            text = self._fetcher(self.url)
            self._ips = {ln.strip() for ln in text.splitlines()
                         if ln.strip() and not ln.strip().startswith("#")}
        return self._ips

    def check(self, address: str) -> SourceResult:
        listed = address in self._load()
        return SourceResult(self.name, listed, self.url if listed else "")
