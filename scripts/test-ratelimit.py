#!/usr/bin/env python3
"""Обмежувач частоти на боці веба (П0.8) — без бази й без FastAPI.

Перевіряється не «спрацьовує чи ні», а три властивості, кожна з яких раз уже
була джерелом помилки в подібному коді:

* ліміт рахується на ОСОБУ, а не на все підряд: сусід по NAT не страждає;
* заборонена спроба НЕ подовжує паузу — інакше той, хто довбиться в закриті
  двері, замикає себе назавжди;
* подія пишеться раз на вікно, а не на кожну відмову.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "front"))
from app.ratelimit import RateLimit  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    print(("  ok  " if cond else "  FAIL") + "  " + name)
    if not cond:
        fails += 1


print("ліміт на особу")
r = RateLimit(3, 60)
check("перші три проходять", [r.hit("a") for _ in range(3)] == [0.0, 0.0, 0.0])
check("четверта чекає", r.hit("a") > 0)
check("сусід по NAT входить далі", r.hit("b") == 0.0)

print("відмова не подовжує паузу")
r = RateLimit(1, 60)
r.hit("a")
first = r.hit("a")
for _ in range(20):
    r.hit("a")
check("після 20 спроб пауза не виросла", r.hit("a") <= first)

print("подія раз на вікно")
r = RateLimit(1, 60)
r.hit("a")
r.hit("a")
check("перша відмова звітує", r.report_once("a") is True)
check("наступні мовчать", not any(r.report_once("a") for _ in range(10)))

print("вікно закінчується")
r = RateLimit(2, 0.2)
r.hit("a")
r.hit("a")
check("зайнято", r.hit("a") > 0)
time.sleep(0.25)
check("через вікно знову можна", r.hit("a") == 0.0)

print("вдалий вхід обнуляє")
r = RateLimit(2, 60)
r.hit("a")
r.hit("a")
r.reset("a")
check("лічильник з нуля", r.hit("a") == 0.0)

print("FAIL" if fails else "усе гаразд")
sys.exit(1 if fails else 0)
