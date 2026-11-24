import csv
import io
import re
import zipfile
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import pandas as pd
import streamlit as st

CENT = Decimal("0.01")

FEC_COLUMNS = [
    "JournalCode", "JournalLib", "EcritureNum", "EcritureDate", "CompteNum",
    "CompteLib", "CompAuxNum", "CompAuxLib", "PieceRef", "PieceDate",
    "EcritureLib", "Debit", "Credit", "EcritureLet", "DateLet", "ValidDate",
    "Montantdevise", "Idevise",
]


def dec(value, default=Decimal("0")):
    if value is None or str(value).strip() == "":
        return default
    s = str(value).strip().replace("\u00a0", "").replace(" ", "")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        return default


def money(value):
    return dec(value).quantize(CENT, rounding=ROUND_HALF_UP)


def date_yyyymmdd(value, dayfirst=False):
    s = str(value or "").strip()
    if not s:
        raise ValueError("date vide")
    for fmt in ("%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt).strftime("%Y%m%d")
        except ValueError:
            pass
    parsed = pd.to_datetime(s, dayfirst=dayfirst, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"date non reconnue : {s}")
    return parsed.strftime("%Y%m%d")


def fec_line(journal, journal_lib, entry_num, date, account, account_lib, piece, label, debit=0, credit=0):
    date = str(date)
    return {
        "JournalCode": journal,
        "JournalLib": journal_lib,
        "EcritureNum": str(entry_num),
        "EcritureDate": date,
        "CompteNum": str(account),
        "CompteLib": str(account_lib),
        "CompAuxNum": "",
        "CompAuxLib": "",
        "PieceRef": str(piece),
        "PieceDate": date,
        "EcritureLib": str(label)[:200],
        "Debit": f"{money(debit):.2f}" if money(debit) else "0.00",
        "Credit": f"{money(credit):.2f}" if money(credit) else "0.00",
        "EcritureLet": "",
        "DateLet": "",
        "ValidDate": date,
        "Montantdevise": "",
        "Idevise": "",
    }


def finish_entry(lines, *, journal, journal_lib, num, date, piece, label,
                 debit_account, debit_label, debits, credits):
    for account, acc_label, amount in debits:
        if money(amount):
            lines.append(fec_line(journal, journal_lib, num, date, account, acc_label, piece, label, debit=amount))
    for account, acc_label, amount in credits:
        if money(amount):
            lines.append(fec_line(journal, journal_lib, num, date, account, acc_label, piece, label, credit=amount))


def summary_csv(file_bytes):
    text = file_bytes.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    header_idx = next((i for i, row in enumerate(rows) if row and row[0].strip().lower() == "date"), None)
    if header_idx is None:
        raise ValueError("Je ne trouve pas la ligne d’en-tête commençant par Date.")
    headers = [h.strip() for h in rows[header_idx]]
    records = []
    for row in rows[header_idx + 1:]:
        if not row or not row[0].strip():
            continue
        rec = {headers[i]: (row[i].strip() if i < len(row) else "") for i in range(len(headers))}
        if rec.get("Date", "").lower() == "total":
            continue
        records.append(rec)
    if not records:
        raise ValueError("Aucune ligne de ventes après l’en-tête Date.")
    return records, headers


def make_summary_fec(file_bytes, rate, journal, accounts):
    rows, headers = summary_csv(file_bytes)
    methods = [h for h in headers if h and h.lower() != "date" and not h.lower().startswith("unnamed")]
    lines, review = [], []
    rate_dec = dec(rate) / Decimal("100")
    for idx, row in enumerate(rows, 1):
        try:
            date = date_yyyymmdd(row.get("Date"), dayfirst=False)
        except ValueError as exc:
            review.append({"ligne": idx, "date": row.get("Date", ""), "motif": str(exc)})
            continue
        debits, total = [], Decimal("0.00")
        for method in methods:
            amount = money(row.get(method))
            if amount < 0:
                review.append({"ligne": idx, "date": row.get("Date", ""), "motif": f"Montant négatif pour {method} : {amount}"})
                continue
            if amount:
                conf = accounts["methods"].get(method, {"account": "580000", "label": method})
                debits.append((conf["account"], conf["label"], amount))
                total += amount
        if not total:
            continue
        vat = money(total * rate_dec / (Decimal("1") + rate_dec))
        ht = total - vat
        piece = f"CS1-{date}-{idx:04d}"
        num = f"CS1{date}{idx:04d}"
        label = f"Ventes quotidiennes {date[6:8]}/{date[4:6]}/{date[:4]}"
        credits = [(accounts["sales_account"], accounts["sales_label"], ht)]
        if vat:
            credits.append((accounts["vat_account"], accounts["vat_label"], vat))
        before = len(lines)
        finish_entry(lines, journal=journal, journal_lib="Ventes récapitulatif", num=num,
                     date=date, piece=piece, label=label,
                     debit_account="", debit_label="", debits=debits, credits=credits)
        if len(lines) > before:
            d = sum((money(x["Debit"]) for x in lines[before:]), Decimal("0"))
            c = sum((money(x["Credit"]) for x in lines[before:]), Decimal("0"))
            delta = d - c
            if delta:
                # Correct rounding on the sales line so each daily entry balances exactly.
                sales_line = next((x for x in lines[before:] if x["CompteNum"] == accounts["sales_account"]), None)
                if sales_line:
                    sales_line["Credit"] = f"{money(dec(sales_line['Credit']) + delta):.2f}"
    return lines, review


VAT_RE = re.compile(r"IMPOT\s+(\d+(?:[.,]\d+)?)\s*%\s+EUR\s+(-?[\d.,]+)", re.I)
TOTAL_HT_RE = re.compile(r"TOTAL\s+HORS\s+TAXE\s+EUR\s+(-?[\d.,]+)", re.I)


def parse_receipt_tax(receipt):
    taxes = {}
    for rate, amount in VAT_RE.findall(receipt or ""):
        r = dec(rate)
        taxes[r] = taxes.get(r, Decimal("0")) + money(amount)
    ht_match = TOTAL_HT_RE.search(receipt or "")
    total_ht = money(ht_match.group(1)) if ht_match else None
    return taxes, total_ht


def infer_tax_bases(taxes, total_ht):
    bases = {r: money(v / (r / Decimal("100"))) for r, v in taxes.items() if r and v}
    if not bases:
        return {}
    if total_ht is not None:
        diff = money(total_ht - sum(bases.values(), Decimal("0")))
        if diff:
            # Printed tax is rounded per ticket; absorb the small residual in the largest base.
            target = max(bases, key=bases.get)
            bases[target] = money(bases[target] + diff)
    return bases


def make_basket_fec(file_bytes, journal, accounts):
    df = pd.read_csv(io.BytesIO(file_bytes), sep="\t", encoding="utf-16", dtype=str).fillna("")
    required = {"BasketId", "ReceiptText", "TransactionTotalInclVat", "SaleDate"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError("Colonnes absentes dans TransactionBasket : " + ", ".join(sorted(missing)))
    lines, review, rate_stats = [], [], {}
    if "IsTransactionDeleted" in df.columns:
        df = df[df["IsTransactionDeleted"].str.lower() != "true"]
    if "TransactionStatusName" in df.columns:
        df = df[df["TransactionStatusName"].str.lower().isin(["completed", "complete", "completed transaction"])]
    for idx, row in df.iterrows():
        basket = str(row.get("BasketId", "")).strip() or f"L{idx + 1}"
        try:
            date = date_yyyymmdd(row.get("SaleDate") or row.get("StartDateTime"), dayfirst=True)
        except ValueError as exc:
            review.append({"BasketId": basket, "date": row.get("SaleDate", ""), "motif": str(exc)})
            continue
        ttc = money(row.get("TransactionTotalInclVat"))
        receipt = row.get("ReceiptText", "")
        taxes, receipt_ht = parse_receipt_tax(receipt)
        if ttc == 0:
            review.append({"BasketId": basket, "date": date, "motif": "Ticket à 0 € (défectueux, déplacé en crédits ou annulé) : aucune écriture générée."})
            continue
        if ttc < 0:
            review.append({"BasketId": basket, "date": date, "motif": f"Ticket négatif ({ttc} €) à traiter manuellement."})
            continue
        if not taxes:
            review.append({"BasketId": basket, "date": date, "motif": "Aucune ligne IMPOT n’a été reconnue dans le reçu."})
            continue
        tax_sum = sum((money(v) for v in taxes.values()), Decimal("0"))
        source_tax = money(row.get("TransactionTotalVat")) if row.get("TransactionTotalVat") else None
        if source_tax is not None and abs(tax_sum - source_tax) >= Decimal("0.02"):
            review.append({"BasketId": basket, "date": date, "motif": f"TVA du reçu {tax_sum:.2f} € différente de la TVA du ticket {source_tax:.2f} €."})
        ht_source = money(row.get("TransactionTotalExclVat")) if row.get("TransactionTotalExclVat") else receipt_ht
        if receipt_ht is not None and ht_source is not None and abs(receipt_ht - ht_source) >= Decimal("0.02"):
            review.append({"BasketId": basket, "date": date, "motif": f"HT du reçu {receipt_ht:.2f} € différent du HT export {ht_source:.2f} €."})
        bases = infer_tax_bases(taxes, ht_source)
        if not bases:
            review.append({"BasketId": basket, "date": date, "motif": "Bases HT impossibles à reconstituer."})
            continue
        # Use the source HT as authoritative and absorb ticket rounding into the largest VAT base.
        bases_total = sum(bases.values(), Decimal("0"))
        remainder = money(ht_source - bases_total) if ht_source is not None else Decimal("0")
        if remainder:
            target = max(bases, key=bases.get)
            bases[target] = money(bases[target] + remainder)
        debits, credits = [], []
        payment_fields = [("TransactionTotalCash", "Espèces"), ("TransactionTotalCashless", "Carte / sans contact"),
                          ("TransactionTotalVouchers", "Bons / vouchers"), ("TransactionTotalPrepaid", "Prépaiement"),
                          ("TransactionTotalCredits", "Crédits")]
        paid = Decimal("0")
        for col, fallback_label in payment_fields:
            amount = money(row.get(col)) if col in df.columns else Decimal("0")
            if amount > 0:
                method = accounts["payment_accounts"].get(col, {"account": "580000", "label": fallback_label})
                debits.append((method["account"], method["label"], amount))
                paid += amount
        if not paid:
            paid = ttc
            method = accounts["payment_accounts"].get("default", {"account": "580000", "label": "Encaissements à ventiler"})
            debits.append((method["account"], method["label"], paid))
        if paid != ttc:
            review.append({"BasketId": basket, "date": date, "motif": f"Règlements {paid:.2f} € différents du TTC {ttc:.2f} € ; écart de {paid - ttc:.2f} €. Vérifier crédits/avoirs."})
            # Balance the ticket to the declared TTC; the account used for the adjustment is configurable.
            adj = money(ttc - paid)
            if adj > 0:
                account = accounts["payment_accounts"].get("default", {"account": "580000", "label": "Encaissements à ventiler"})
                debits.append((account["account"], account["label"], adj))
            elif adj < 0:
                # Reverse excess from the last payment line, leaving the review visible.
                for k in range(len(debits) - 1, -1, -1):
                    a, l, v = debits[k]
                    cut = min(v, -adj)
                    debits[k] = (a, l, money(v - cut))
                    adj = money(adj + cut)
                    if not adj:
                        break
        for rate, base in bases.items():
            if base:
                acct = accounts["rates"].get(str(rate), {"sales_account": "707000", "sales_label": f"Ventes TVA {rate}%", "vat_account": "445710", "vat_label": f"TVA collectée {rate}%"})
                credits.append((acct["sales_account"], acct["sales_label"], base))
                vat = money(taxes.get(rate, 0))
                if vat:
                    credits.append((acct["vat_account"], acct["vat_label"], vat))
                stt = rate_stats.setdefault(str(rate), {"ht": Decimal("0"), "vat": Decimal("0"), "ttc": Decimal("0"), "tickets": 0})
                stt["ht"] += base
                stt["vat"] += vat
                stt["ttc"] += base + vat
                stt["tickets"] += 1
        piece = f"CS2-{date}-{basket}"
        num = f"CS2{date}{re.sub(r'[^A-Za-z0-9]', '', basket)[-12:]}"
        label = f"Vente ticket {basket}"
        before = len(lines)
        finish_entry(lines, journal=journal, journal_lib="Ventes tickets", num=num,
                     date=date, piece=piece, label=label,
                     debit_account="", debit_label="", debits=debits, credits=credits)
        if len(lines) > before:
            dtotal = sum((money(x["Debit"]) for x in lines[before:]), Decimal("0"))
            ctotal = sum((money(x["Credit"]) for x in lines[before:]), Decimal("0"))
            delta = money(dtotal - ctotal)
            if delta:
                # If ticket rounding creates a cent gap, adjust the largest sales credit.
                sale_accounts = {v["sales_account"] for v in accounts["rates"].values()}
                candidates = [x for x in lines[before:] if x["CompteNum"] in sale_accounts]
                if candidates:
                    target = max(candidates, key=lambda x: dec(x["Credit"]))
                    target["Credit"] = f"{money(dec(target['Credit']) + delta):.2f}"
    return lines, review, rate_stats


def fec_bytes(lines):
    df = pd.DataFrame(lines, columns=FEC_COLUMNS)
    buf = io.StringIO(newline="")
    df.to_csv(buf, sep="|", index=False, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    return buf.getvalue().encode("utf-8-sig")


def to_csv_bytes(rows):
    if not rows:
        return "Aucune anomalie\n".encode("utf-8-sig")
    return pd.DataFrame(rows).to_csv(index=False, sep=";", decimal=",", encoding="utf-8-sig").encode("utf-8-sig")


def add_rate_controls(rates):
    config = {}
    cols = st.columns(max(1, len(rates)))
    for i, r in enumerate(rates):
        base = str(r).replace(".", "")
        default_accounts = {"5.5": ("7071055", "44571055"), "10": ("7071010", "44571010"), "20": ("7071020", "44571020")}
        sales_default, vat_default = default_accounts.get(str(r), (f"7071{base}", f"44571{base}"))
        with cols[i % len(cols)]:
            st.markdown(f"**Taux {r}%**")
            config[str(r)] = {
                "sales_account": st.text_input(f"Compte 707 – {r}%", value=sales_default, key=f"sale_{r}"),
                "sales_label": st.text_input(f"Libellé ventes – {r}%", value=f"Ventes TVA {r}%", key=f"slab_{r}"),
                "vat_account": st.text_input(f"Compte TVA – {r}%", value=vat_default, key=f"vat_{r}"),
                "vat_label": st.text_input(f"Libellé TVA – {r}%", value=f"TVA collectée {r}%", key=f"vlab_{r}"),
            }
    return config


st.set_page_config(page_title="FEC ventes CS1 / CS2", page_icon="🧾", layout="wide")
st.title("Conversion caisse → FEC importable")
st.caption("Deux journaux séparés : récapitulatif journalier en CS1 et tickets ventilés par taux de TVA en CS2.")
st.info("Les montants sont traités comme deux flux autonomes. Les fichiers chargés présentent des périmètres différents ; ne les importez pas tous les deux si l’un reprend déjà les ventes de l’autre.")

with st.expander("1. Récapitulatif journalier → journal CS1", expanded=True):
    file1 = st.file_uploader("CSV récapitulatif du chiffre d’affaires", type=["csv"], key="summary")
    c1, c2, c3 = st.columns(3)
    with c1:
        rate1 = st.number_input("Taux de TVA appliqué (%)", min_value=0.0, max_value=100.0, value=5.5, step=0.1)
        journal1 = st.text_input("Journal", "CS1", max_chars=6)
    with c2:
        sales1 = st.text_input("Compte de ventes", "707100")
        vat1 = st.text_input("Compte de TVA collectée", "44571055")
    with c3:
        saleslabel1 = st.text_input("Libellé compte ventes", "Ventes caisse CS1")
        vatlabel1 = st.text_input("Libellé compte TVA", "TVA collectée CS1")
    if file1:
        try:
            records1, headers1 = summary_csv(file1.getvalue())
            methods = [x for x in headers1 if x and x.lower() != "date" and not x.lower().startswith("unnamed")]
            st.markdown("**Comptes de contrepartie par moyen de règlement**")
            method_accounts = {}
            cols = st.columns(max(1, len(methods)))
            defaults = {"espèces": ("530000", "Caisse"), "carte bancaire": ("511200", "CB à encaisser"), "carte fidélité": ("580000", "Compte de liaison carte fidélité")}
            for i, method in enumerate(methods):
                acc, lbl = defaults.get(method.lower(), ("580000", method))
                with cols[i % len(cols)]:
                    method_accounts[method] = {"account": st.text_input(f"Compte – {method}", acc, key=f"m1a_{method}"),
                                               "label": st.text_input(f"Libellé – {method}", lbl, key=f"m1l_{method}")}
            if st.button("Préparer le FEC CS1", key="go1"):
                lines1, review1 = make_summary_fec(file1.getvalue(), rate1, journal1,
                    {"sales_account": sales1, "sales_label": saleslabel1, "vat_account": vat1,
                     "vat_label": vatlabel1, "methods": method_accounts})
                if lines1:
                    st.success(f"{len(lines1)} lignes prêtes — journal {journal1}.")
                    st.download_button("Télécharger le FEC CS1", fec_bytes(lines1), "ventes_CS1.fec", "text/plain", key="dl1")
                    if review1:
                        st.warning(f"{len(review1)} ligne(s) du récapitulatif à vérifier.")
                        st.download_button("Télécharger les vérifications CS1", to_csv_bytes(review1), "verifications_CS1.csv", "text/csv", key="rv1")
                else:
                    st.warning("Aucune ligne de vente positive à exporter.")
        except Exception as exc:
            st.error(f"Lecture impossible : {exc}")

with st.expander("2. Détail des tickets et TVA lue sur les reçus → journal CS2", expanded=True):
    file2 = st.file_uploader("TransactionBasket.csv (UTF-16, séparateur tabulation)", type=["csv", "txt"], key="basket")
    if file2:
        try:
            preview = pd.read_csv(io.BytesIO(file2.getvalue()), sep="\t", encoding="utf-16", dtype=str).fillna("")
            if "ReceiptText" not in preview.columns:
                st.error("Ce fichier ne semble pas être l’export TransactionBasket attendu (colonne ReceiptText absente).")
            else:
                discovered = set()
                for txt in preview["ReceiptText"].head(10000):
                    discovered.update(dec(x) for x in re.findall(r"IMPOT\s+(\d+(?:[.,]\d+)?)\s*%", txt, re.I))
                rates = sorted(discovered) or [Decimal("5.5"), Decimal("10")]
                journal2 = st.text_input("Journal", "CS2", max_chars=6)
                payment_accounts = {}
                st.markdown("**Comptes d’encaissement**")
                pay_fields = [("TransactionTotalCash", "Espèces", "530000"), ("TransactionTotalCashless", "Carte / sans contact", "511200"),
                              ("TransactionTotalVouchers", "Bons / vouchers", "580000"), ("TransactionTotalPrepaid", "Prépaiement", "580000"),
                              ("TransactionTotalCredits", "Crédits", "580000"), ("default", "Règlement à ventiler", "580000")]
                cols = st.columns(3)
                for i, (key, lbl, default) in enumerate(pay_fields):
                    with cols[i % 3]:
                        payment_accounts[key] = {"account": st.text_input(f"Compte {lbl}", default, key=f"p2_{key}"), "label": lbl}
                rate_accounts = add_rate_controls(rates)
                if st.button("Préparer le FEC CS2", key="go2"):
                    lines2, review2, stats2 = make_basket_fec(file2.getvalue(), journal2,
                        {"rates": rate_accounts, "payment_accounts": payment_accounts})
                    if lines2:
                        st.success(f"{len(lines2)} lignes prêtes — journal {journal2}.")
                        st.dataframe(pd.DataFrame([{"Taux": f"{r}%", "Tickets concernés": s["tickets"],
                            "HT": float(s["ht"]), "TVA reçus": float(s["vat"]), "HT + TVA": float(s["ttc"])} for r, s in stats2.items()]), hide_index=True)
                        st.download_button("Télécharger le FEC CS2", fec_bytes(lines2), "ventes_CS2.fec", "text/plain", key="dl2")
                    if review2:
                        st.warning(f"{len(review2)} ticket(s) nécessitent une vérification. Les écarts et tickets à 0 € ne sont pas masqués.")
                        st.dataframe(pd.DataFrame(review2), hide_index=True, use_container_width=True)
                        st.download_button("Télécharger les vérifications CS2", to_csv_bytes(review2), "verifications_CS2.csv", "text/csv", key="rv2")
                    if not lines2:
                        st.warning("Aucune écriture complète n’a pu être produite. Consultez les vérifications.")
        except Exception as exc:
            st.error(f"Lecture impossible : {exc}")

st.divider()
st.caption("Le fichier généré reprend les colonnes FEC (séparateur |, dates AAAAMMJJ). Contrôlez les comptes de caisse/encaissement, les comptes 707/44571 et les tickets signalés avant import.")
