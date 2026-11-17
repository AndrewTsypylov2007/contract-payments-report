#!/usr/bin/env python3
"""Отчёт по оплате работ и договоров на заданную дату.

Запуск:  python3 report.py --date 10.11.2026 [--data data]
Только стандартная библиотека, деньги считаются в Decimal.
"""
import argparse
import csv
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

ZERO = Decimal("0.00")


def parse_date(s):
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"не понимаю дату: {s!r}")


def parse_money(s):
    return Decimal(s.strip()).quantize(Decimal("0.01"))


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


@dataclass
class Contract:
    id: str
    contractor: str
    amount: Decimal
    signed_at: date


@dataclass
class Work:
    id: str
    contract_id: str
    title: str
    price: Decimal
    completed_at: date
    cancelled_at: date
    # заполняется при расчёте
    state: str = "planned"
    paid_direct: Decimal = ZERO   # платежи, привязанные к работе
    paid_pool: Decimal = ZERO     # часть платежей по договору, отнесённая на работу

    @property
    def paid(self):
        return self.paid_direct + self.paid_pool


@dataclass
class Payment:
    id: str
    contract_id: str
    work_id: str
    purpose: str
    amount: Decimal
    created_at: date
    paid_at: date
    refunded_at: date
    state: str = ""


def load(data_dir):
    p = Path(data_dir)
    contracts = {
        r["contract_id"]: Contract(r["contract_id"], r["contractor"],
                                   parse_money(r["amount"]), parse_date(r["signed_at"]))
        for r in read_csv(p / "contracts.csv")
    }
    works = {
        r["work_id"]: Work(r["work_id"], r["contract_id"], r["title"],
                           parse_money(r["price"]), parse_date(r["completed_at"]),
                           parse_date(r["cancelled_at"]))
        for r in read_csv(p / "works.csv")
    }
    payments = [
        Payment(r["payment_id"], r["contract_id"], r["work_id"].strip(), r["purpose"],
                parse_money(r["amount"]), parse_date(r["created_at"]),
                parse_date(r["paid_at"]), parse_date(r["refunded_at"]))
        for r in read_csv(p / "payments.csv")
    ]
    return contracts, works, payments


def work_state(w, asof):
    """Статус работы на дату (а не на момент выгрузки)."""
    if w.cancelled_at and w.cancelled_at <= asof:
        return "cancelled"
    if w.completed_at and w.completed_at <= asof:
        return "done"
    return "planned"


def payment_state(p, asof):
    """paid - деньги у подрядчика; returned - ушли и вернулись;
    queued - заведён, но не оплачен; future - на дату ещё не существовал."""
    if p.paid_at and p.paid_at <= asof:
        if p.refunded_at and p.refunded_at <= asof:
            return "returned"
        return "paid"
    if p.created_at and p.created_at <= asof:
        return "queued"
    return "future"


@dataclass
class ContractResult:
    contract: Contract
    works: list
    active_total: Decimal = ZERO      # цена работ без отменённых
    cancelled_total: Decimal = ZERO
    paid: Decimal = ZERO              # реально у подрядчика на дату
    returned: Decimal = ZERO          # ушло и вернулось к дате
    queued: Decimal = ZERO
    remaining: Decimal = ZERO         # осталось оплатить по неотменённым работам
    due_now: Decimal = ZERO           # из них по уже принятым работам
    overpaid: Decimal = ZERO          # оплачено сверх цены работ / по отменённым
    warnings: list = field(default_factory=list)


def build(contracts, works, payments, asof):
    warnings_global = []
    for w in works.values():
        w.state = work_state(w, asof)
        w.paid_direct = w.paid_pool = ZERO
        if w.contract_id not in contracts:
            warnings_global.append(f"Работа {w.id}: неизвестный договор {w.contract_id}")
    for p in payments:
        p.state = payment_state(p, asof)
        if p.contract_id not in contracts:
            warnings_global.append(f"Платёж {p.id}: неизвестный договор {p.contract_id}")
        if p.work_id and p.work_id not in works:
            warnings_global.append(f"Платёж {p.id}: неизвестная работа {p.work_id}")
        elif p.work_id and works[p.work_id].contract_id != p.contract_id:
            warnings_global.append(
                f"Платёж {p.id}: работа {p.work_id} принадлежит другому договору")

    results = []
    for c in contracts.values():
        cw = [w for w in works.values() if w.contract_id == c.id]
        cp = [p for p in payments if p.contract_id == c.id]
        r = ContractResult(c, cw)
        r.cancelled_total = sum((w.price for w in cw if w.state == "cancelled"), ZERO)
        r.active_total = sum((w.price for w in cw if w.state != "cancelled"), ZERO)
        r.paid = sum((p.amount for p in cp if p.state == "paid"), ZERO)
        r.returned = sum((p.amount for p in cp if p.state == "returned"), ZERO)
        r.queued = sum((p.amount for p in cp if p.state == "queued"), ZERO)

        # 1) платежи, привязанные к работе, идут на эту работу
        pool = ZERO
        for p in cp:
            if p.state != "paid":
                continue
            w = works.get(p.work_id) if p.work_id else None
            if w is None:
                pool += p.amount
            elif w.state == "cancelled":
                w.paid_direct += p.amount
                r.warnings.append(
                    f"Платёж {p.id} ({p.amount}) привязан к отменённой работе {w.id}: "
                    f"нужен возврат или перенос")
            else:
                take = min(p.amount, w.price - w.paid_direct)
                w.paid_direct += take
                if p.amount > take:
                    pool += p.amount - take
                    r.warnings.append(
                        f"Платёж {p.id} больше цены работы {w.id}; "
                        f"излишек {p.amount - take} отнесён на договор")

        # 2) платежи без привязки закрывают работы по очереди:
        #    сначала принятые (по дате приёмки), потом запланированные (по номеру)
        done = sorted((w for w in cw if w.state == "done"),
                      key=lambda w: (w.completed_at, w.id))
        planned = sorted((w for w in cw if w.state == "planned"), key=lambda w: w.id)
        for w in done + planned:
            take = min(pool, w.price - w.paid_direct)
            w.paid_pool = take
            pool -= take

        cancelled_paid = sum((w.paid for w in cw if w.state == "cancelled"), ZERO)
        r.overpaid = pool + cancelled_paid
        for w in cw:
            if w.state != "cancelled":
                rem = w.price - w.paid
                r.remaining += rem
                if w.state == "done":
                    r.due_now += rem

        # проверки согласованности
        all_total = sum((w.price for w in cw), ZERO)
        if all_total != c.amount:
            r.warnings.append(
                f"Сумма договора {c.amount} не равна сумме цен работ {all_total}")
        if pool > 0:
            r.warnings.append(f"Оплачено сверх цены работ: {pool}")
        if r.queued > 0 and r.queued != r.remaining:
            r.warnings.append(
                f"В очереди {r.queued}, а осталось оплатить {r.remaining}: "
                f"после оплаты очереди разница {r.remaining - r.queued}")
        results.append(r)
    return results, warnings_global


STATE_RU = {"done": "принята", "planned": "не принята", "cancelled": "отменена"}


def fmt(x):
    return f"{x:,.2f}".replace(",", " ")


def table(headers, rows):
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)]
    def line(cells):
        return "  ".join(
            str(c).ljust(wd) if i < 3 else str(c).rjust(wd)
            for i, (c, wd) in enumerate(zip(cells, widths)))
    out = [line(headers), "  ".join("-" * wd for wd in widths)]
    out += [line(r) for r in rows]
    return "\n".join(out)


def render(results, warnings_global, payments, asof):
    out = [f"ОТЧЁТ ПО ОПЛАТЕ РАБОТ НА {asof.strftime('%d.%m.%Y')}", ""]
    for r in results:
        c = r.contract
        out.append(f"Договор {c.id} - {c.contractor} (сумма договора {fmt(c.amount)})")
        rows = []
        for w in sorted(r.works, key=lambda w: w.id):
            rem = ZERO if w.state == "cancelled" else w.price - w.paid
            rows.append((w.id, w.title, STATE_RU[w.state], fmt(w.price),
                         fmt(w.paid), fmt(rem)))
        out.append(table(("Работа", "Название", "Статус", "Цена", "Выплачено",
                          "Осталось"), rows))
        out.append("")
        out.append(f"  Работы без отменённых:             {fmt(r.active_total)}"
                   + (f"  (отменено на {fmt(r.cancelled_total)})" if r.cancelled_total else ""))
        out.append(f"  Выплачено подрядчику:              {fmt(r.paid)}")
        out.append(f"  Осталось оплатить всего:           {fmt(r.remaining)}")
        out.append(f"    из них по принятым работам:      {fmt(r.due_now)}")
        out.append(f"  Оплачено сверх / по отменённым:    {fmt(r.overpaid)}")
        out.append(f"  В очереди на оплату (не оплачено): {fmt(r.queued)}")
        if r.returned:
            out.append(f"  Выплачено и возвращено к дате:     {fmt(r.returned)} (в выплаченное не входит)")
        for wmsg in r.warnings:
            out.append(f"  ! {wmsg}")
        out.append("")
    ignored = [p for p in payments if p.state == "future"]
    if ignored:
        out.append("Не учтены, т.к. на дату отчёта их ещё не было: "
                   + ", ".join(p.id for p in ignored))
    for wmsg in warnings_global:
        out.append(f"! {wmsg}")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Отчёт по оплате работ и договоров")
    ap.add_argument("--date", default=date.today().isoformat(),
                    help="дата отчёта: ДД.ММ.ГГГГ или ГГГГ-ММ-ДД (по умолчанию сегодня)")
    ap.add_argument("--data", default="data", help="папка с CSV (по умолчанию data)")
    args = ap.parse_args(argv)
    asof = parse_date(args.date)
    contracts, works, payments = load(args.data)
    results, wg = build(contracts, works, payments, asof)
    print(render(results, wg, payments, asof))


if __name__ == "__main__":
    sys.exit(main())
