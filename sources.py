"""Registry for installed, trusted public data source plugins."""
import ipaddress
import re
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Callable
from urllib.parse import urlencode, urlsplit


def domain(value):
    value = value.strip().rstrip(".").lower().encode("idna").decode()
    labels = value.split(".")
    if len(value) > 253 or len(labels) < 2 or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in labels
    ):
        raise ValueError("Введите доменное имя")
    return value


def dns(value):
    return "https://dns.google/resolve?" + urlencode({"name": domain(value), "type": "A"})


def ct(value):
    return "https://crt.sh/?" + urlencode({"q": "%." + domain(value), "output": "json"})


def rdap(value):
    address = ipaddress.ip_address(value.strip())
    if not address.is_global:
        raise ValueError("Введите публичный IP-адрес")
    return f"https://rdap.org/ip/{address}"


def check_dns(data):
    if not isinstance(data, dict) or type(data.get("Status")) is not int:
        raise ValueError("Источник DNS вернул некорректный ответ")
    if data["Status"] not in (0, 3):
        raise ValueError(f'Ошибка DNS, код {data["Status"]}')


def check_ct(data):
    if not isinstance(data, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("name_value"), str) for row in data
    ):
        raise ValueError("CT вернул некорректный список сертификатов")


def check_rdap(data):
    if not isinstance(data, dict) or data.get("objectClassName") != "ip network":
        raise ValueError("RDAP не вернул запись IP-сети")


@dataclass(frozen=True)
class Source:
    id: str
    title: str
    description: str
    input_label: str
    host: str
    url: Callable[[str], str]
    check: Callable[[object], None]
    min_interval: float = 1.0
    key_env: str = ""
    key_header: str = "Authorization"
    key_prefix: str = "Bearer "
    follow_redirects: bool = False

    def endpoint(self, value):
        url = self.url(value)
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.netloc != self.host
                or parts.username is not None or parts.fragment):
            raise ValueError("URL не соответствует HTTPS-адресу источника")
        return url


BUILTINS = (
    Source("dns-a", "DNS · A", "Публичные A-записи Google DNS", "Домен", "dns.google", dns, check_dns),
    Source("ct", "Certificate Transparency", "Публичный индекс crt.sh", "Домен", "crt.sh", ct, check_ct, 2),
    Source("ip-rdap", "RDAP · IP", "Регистрационные данные публичной сети", "Публичный IP",
           "rdap.org", rdap, check_rdap, follow_redirects=True),
)


def discover():
    registry = {source.id: source for source in BUILTINS}
    errors = []
    for entry in entry_points(group="nexus_osint.sources"):
        try:
            source = entry.load()
            if not isinstance(source, Source) or source.id != entry.name or source.id in registry:
                raise ValueError("Некорректный или повторяющийся идентификатор")
            if not source.host or not 0 < source.min_interval <= 30:
                raise ValueError("Укажите HTTPS-хост и интервал от 0 до 30 секунд")
            if source.key_env and source.follow_redirects:
                raise ValueError("Перенаправления запрещены для источников с ключом")
            registry[source.id] = source
        except Exception as error:
            errors.append(f"{entry.name}: {type(error).__name__}")
    return registry, errors
