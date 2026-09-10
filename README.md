# eTIMS Integration

KRA eTIMS fiscalisation for ERPNext, through the **Comstore Smart VSCU** service
(ComstoreFC4Api 3.4.2).

ERPNext talks to a Comstore service on the LAN, which drives the FC4 fiscal
device, which transmits to KRA:

```
Sales Invoice ──▶ ETIMS Transmission ──▶ ComstoreApiService ──▶ FC4 device ──▶ KRA
                   (queue + audit)         (:4000, X-API-KEY)
```

## Design in one page

**Nothing talks to a device inside a submit transaction.** `on_submit` creates an
`ETIMS Transmission` and returns. A worker does the sending. A slow, rebooting or
dead device costs the cashier nothing, and every failure has somewhere to live.

**The Transmission is the only unit of work.** Queue, retry budget, signature,
failure and its remedy are one row. "Which invoices still owe KRA a signature?"
is a list view filter, not an Error Log search.

**Tax bands are declared, never guessed.** A band is resolved from the Item, then
from an `ETIMS Tax Mapping` on its Item Tax Template, then from the Item Group.
If none answers, the invoice is *blocked* — never defaulted to 16%. A wrong band
is rejected by eTIMS (E321) *and* misdeclares tax.

**Line totals are authoritative.** Band net/tax figures are derived by decomposing
the very `SaleAmount` that goes into `plu_data`, so `sum(bands) == sum(lines)`
holds by construction. That makes E341 and E322 structurally unreachable rather
than merely unlikely. VAT is computed from the rate and any leftover cent is
pushed into the levy, which reproduces KRA's own worked examples exactly.

**Retry only what retrying can fix.** Transport faults, a dropped FC4 link and the
service's own negative failure codes retry with exponential backoff. A payload the
device *rejected* is parked as `Blocked` with the operator remedy attached — the
device is deterministic, so re-sending identical bytes just earns an identical
rejection. Fixing the cause and pressing **Retry** restores a full retry budget;
without that a resend got one attempt and fell straight back into `Blocked`.

**A fault is read three ways, because it is spelled three ways.** The vendor's
headings say `E337`, complete-workflow says `NO FIND PLU DATA (Code 337)` inside a
sentence, and the status endpoints put a bare `-99` in a field. All three resolve to
the same `ErrorSpec`. A bare number only counts as a code where something says it is
one, so a trader invoice number ending 341 is not read as E341.

**The invoice always says what its Transmission says.** Every state change mirrors
onto the Sales Invoice, not just a signature. An invoice reading `Queued` while its
Transmission is `Blocked` is the one failure mode nobody investigates.

## Layers

| Path | Responsibility | Imports Frappe? |
|---|---|---|
| `comstore/` | HTTP, `X-API-KEY`, error taxonomy, wire shapes | **no** |
| `mapping/` | ERPNext document → payload. Pure functions | yes (reads only) |
| `services/` | Queue, state machine, retries, sync, reconciliation | yes |
| `overrides/` | `doc_events`. Thin: validate or enqueue, nothing else | yes |

`comstore/` deliberately imports no Frappe, so the rules that decide what KRA is
told are testable without a site — and so the transport can be swapped for KRA's
direct OSCU API without touching the mapping.

## Setup

```bash
bench get-app etims_integration
bench --site <site> install-app etims_integration
```

Then:

1. **eTIMS Settings** — leave *Test Mode* on for now. Map each Item Tax Template
   to its band under *Tax Bands*.
2. **eTIMS Device** — one per company (or branch). For the shared test service:
   host `hedgeinc.co.ke`, no port, API key from the vendor. For an on-premise
   install: host `192.168.1.x`, port `4000`. Hit **Test Connection**.
3. **Items** — set the eTIMS classification, tax type, packaging and quantity unit,
   then *Register with eTIMS* from the Item list. Nothing can be fiscalised until
   its items are registered on the device (E337).
4. Submit a test invoice, then open **Preview Payload** on it to see exactly what
   would be declared before anything is.
5. Turn **Test Mode** off on the device only when you are ready to trade live.

### Test mode

`is_test: true` makes the device sign locally and send **nothing** to KRA. Test
signatures look identical to real ones, so the invoice form, the device form and
the print format all say so explicitly.

## Operating it

- **eTIMS workspace** → Transmissions, filtered by status. Select rows in the list
  and use *Retry Selected*, or **Retry Blocked Transmissions** on the device — one
  unregistered item or one offline morning blocks many invoices at once, and the fix
  is almost always shared.
- **Transmission → Device Exchanges** lists every attempt. The Transmission carries
  only the latest one; each retry overwrites its response.
- **Reconciliation** runs hourly: it reads each device's backlog of invoices KRA
  has not acknowledged, forces an upload when the backlog stops shrinking, and
  raises an alert if it does not recover. A signature is not compliance — the
  device forwards on its own schedule and the vendor documentation is explicit
  that this "may not be the case 100% of the time".
- `reconcile.unsigned_invoices()` answers the month-end question directly.
- A **fiscalised invoice cannot be cancelled** by default. A signature cannot be
  withdrawn from KRA; the compliant reversal is a credit note.

## Tests

The layers that need no site run under plain unittest:

```bash
cd ~/frappe-bench
./env/bin/python -m unittest discover \
    -s apps/etims_integration/etims_integration/tests \
    -t apps/etims_integration
```

These cover the tax decomposition against every worked example in the vendor
documentation, plus a property check that the parts always re-add to the total
across awkward amounts, rates and levies.

## Known divergences from the vendor documentation

Checked against Comstore API Documentation 3.4.2 and kept here so the next reader
does not re-derive them:

- **`GET /api/invoices/status/{sn}`.** The endpoint table (p. 4) says GET; the
  section body (p. 30) says "Method: POST" one line below "This endpoint sends a GET
  request". The live service answers GET, so GET is what is sent. A build that
  disagrees now reports `NO_ENDPOINT` rather than an unrecognised device error.
- **`DiscAmt` in `sign_structure`.** Absent from the parameter table, present in both
  of the vendor's own Postman examples, so it is sent — an undocumented field the
  vendor always sends is likelier to be expected than ignored.
- **Negative `error_code` values.** Only `-1` is shown (p. 31). `-99` is real and
  comes out of the invoice-status endpoint. Both are treated as service faults, not
  payload rejections, so they retry.
- **`transmit_status`** appears in live complete-workflow replies and in no version
  of the document. It is captured in `response_json` and read by nothing.

## Reference

- Comstore API Documentation 3.4.2 (Dejavu Technology Solutions)
- Postman collection: <https://documenter.getpostman.com/view/33982185/2sBXcKDe3k>
- KRA item classification codes:
  <https://docs.google.com/spreadsheets/d/1g3Xm0g6rgLNVp8h5paTednaBRkfzppnTulqg2OY5xNI>

#### License

MIT
