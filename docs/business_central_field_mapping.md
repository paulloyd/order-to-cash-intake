# Business Central mapping (API v2.0 `salesOrders`)

| Pipeline field | BC field | Notes |
|---|---|---|
| customer account (C-1007) | `customerNumber` | resolved from sender email, domain, account number, or name |
| customer PO | `externalDocumentNumber` | doubles as the idempotency key: BC (and the mock) reject a repeat per customer |
| requested/default date | `requestedDeliveryDate` | default = received + 10 days when none given |
| line sku | `salesOrderLines[].lineObjectNumber` with `lineType: "Item"` | customer part numbers are translated via cross-reference first |
| qty | `salesOrderLines[].quantity` | validated against MOQ and pack multiple |
| contract price | `salesOrderLines[].unitPrice` | list price less customer discount; variances need approval |

Flow in live mode: `POST .../salesOrders` (header) then `POST .../salesOrders({id})/salesOrderLines` per line.
Shipments are polled from `salesShipments` and matched to the order number.

Not modeled: reservations/available-to-promise (stock is checked but not reserved), multi-currency, tax, dimensions, item variants.
