# Stax endpoints verified against the specification on 2026-09-30

Source: `https://docs.staxpayments.com/reference/<page>.md`, the OpenAPI block embedded in each
page, read by Fable on 2026-09-30. Base URL in every block: `https://apiprod.fattlabs.com/`.
Anything not in this file is unverified and must be `TODO-VERIFY` in code.

| Interface method | Verified call | Page |
|---|---|---|
| create_customer | `POST /customer` | create-customer |
| update_customer | `PUT /customer/{id}` | update-customer-info |
| get_customer | `GET /customer/{id}` | get-a-customers-information |
| find_customers | `GET /customer` with query filters | find-all-customers |
| create_invoice | `POST /invoice` | create-an-invoice |
| update_invoice | `PUT /invoice/{id}` | update-an-invoice |
| get_invoice | `GET /invoice/{id}` | get-an-invoice |
| list_invoices | `GET /invoice` | get-all-invoices |
| delete_invoice | `DELETE /invoice/{id}` | delete-an-invoice |
| send_invoice_email | `PUT /invoice/{id}/send/email`, body `ccEmails` | send-an-invoice-via-email |
| mark_invoice_paid_cash_or_check | `POST /invoice/{id}/pay/{method}` | mark-an-invoice-paid-in-cash-or-check |
| pay_invoice (flagged off) | `POST /invoice/{id}/pay`, query `payment_method_id`, `idempotency_id`, `apply_balance`, `email_receipt`, `funding` | pay-an-invoice |
| list_transactions | `GET /transaction` | list-and-filter-all-transactions |
| get_transaction | `GET /transaction/{id}` | get-a-transactions-information |
| refund_transaction | `POST /transaction/{id}/refund`, body `total` (string, required) | refund-transaction |
| void_transaction | `POST /transaction/{id}/void` | void-transaction |
| list_deposits | `GET /query/deposit`, query `startDate`*, `endDate`*, `timezone`, `timeZoneOffset` | get-list-of-deposits |
| get_deposit | `GET /query/depositDetail`, query `startDate`*, `endDate`*, `batchId` | get-detail-of-specific-deposit |
| create_scheduled_invoice | `POST /invoice/schedule/` | create-a-scheduled-invoice |
| list_scheduled_invoices | `GET /invoice/schedule`, query `customer_id`, `sort`, `order` | retrieve-all-scheduled-invoices |
| update_scheduled_invoice | `PUT /invoice/schedule/{id}`, body `active`, `rule`, `email_notification` (also `total`, `meta`: never sent by this app) | edit-an-invoice-schedule |
| delete_scheduled_invoice | `DELETE /invoice/schedule/{id}` | delete-invoice-schedule |
| list_ach_rejections | `GET /query/statement/v3/ach-rejects`, query `startDate`*, `endDate`* | ach-rejections |
| ping (authentication check) | `GET /self` | get-self-and-teams-info |

**Not found in the specification (TODO-VERIFY):**
- A `GET /invoice/schedule/{id}` for one schedule. Only the list exists. `get_scheduled_invoice`
  is implemented as the list filtered by ID until verified (TODO-VERIFY-020).
- The accepted values of `{method}` on mark-paid. Text says cash, check, gift card; the exact
  strings are not in the block (TODO-VERIFY-016).
- Pagination parameters on `GET /invoice`, `GET /customer`, `GET /invoice/schedule` (only
  `GET /transaction` lists `per_page` and `page`) (TODO-VERIFY-021).
- The response schema of ACH rejections beyond `ach_reject_transaction_id`,
  `ach_reject_transaction_created_at`, `ach_reject_transaction_amount` (TODO-VERIFY-022).
- Whether `GET /deposit/{id}` exists; the spec offers `depositDetail` by date and batch only.

## Field names extracted from the specification blocks

```
== find-all-customers: GET /customer
   params: firstname(query), lastname(query), email(query), company(query), phone(query), address_1(query), address_2(query), address_city(query), address_state(query), address_zip(query), address_country(query), notes(query), reference(query), files(query), cc_emails(query), cc_sms(query), allow_invoice_credit_card_payments(query), keywords[](query)
== create-customer: POST /customer
   body: firstname:string; lastname:string; email:string; company:string; phone:string; address_1:string; address_2:string; address_city:string; address_state:string; address_zip:string; address_country:string; notes:string; reference:string; files:array; cc_emails:array; cc_sms:array; allow_invoice_credit_card_payments:boolean; options:object; options.customer_custom_fields:array; options.customer_custom_fields[].id:string; options.customer_custom_fields[].name:string; options.customer_custom_fields[].placeholder:string; options.customer_custom_fields[].required:boolean; options.customer_custom_fields[].visible:boolean; options.customer_custom_fields[].type:string enum=['text']; options.customer_custom_fields[].value:string
== update-customer-info: PUT /customer/{id}
   params: id(path)*
   body: firstname:string; lastname:string; email:string; company:string; phone:string; address_1:string; address_2:string; address_city:string; address_state:string; address_zip:string; address_country:string; notes:string; reference:string; files:array; cc_emails:array; cc_sms:array; allow_invoice_credit_card_payments:boolean; options:object; options.customer_custom_fields:array; options.customer_custom_fields[].id:string; options.customer_custom_fields[].name:string; options.customer_custom_fields[].placeholder:string; options.customer_custom_fields[].required:boolean; options.customer_custom_fields[].visible:boolean; options.customer_custom_fields[].type:string enum=['text']; options.customer_custom_fields[].value:string
== list-and-filter-all-transactions: GET /transaction
   params: start_date(query), end_date(query), type(query), status(query), method(query), customer_id(query), payment_method_id(query), minTotal(query), maxTotal(query), pre_auth(query), is_captured(query), is_suspected_fraud(query), per_page(query), page(query), sort(query), order(query), currency(query), export(query)
== get-a-transactions-information: GET /transaction/{id}
   params: id(path)*
== refund-transaction: POST /transaction/{id}/refund
   params: id(path)*
   body: total*:string
== void-transaction: POST /transaction/{id}/void
   params: id(path)*
== create-an-invoice: POST /invoice
   body: customer_id*:string; url*:string; invoice_date_at:string; due_at:string; meta:object; meta.subtotal:string; meta.tax:string; meta.lineItems:array; meta.lineItems[].item:string; meta.lineItems[].price:number; meta.lineItems[].quantity:number; meta.lineItems[].details:string; meta.lineItems[].id:string; meta.lineItems[].productCode:string; meta.lineItems[].commodityCode:string; meta.lineItems[].unitOfMeasure:string; meta.lineItems[].tax:number; meta.lineItems[].taxRate:number; meta.lineItems[].discountRate:number; meta.lineItems[].total:number; meta.memo:string; meta.reference:string; meta.internalMemo:string; meta.isCCPaymentEnabled:boolean; meta.isACHPaymentEnabled:boolean; meta.isTipEnabled:string; meta.successRedirect:string; meta.iscnpdigitalwallet:boolean; is_partial_payment_enabled:boolean; funding:array; total*:number
== update-an-invoice: PUT /invoice/{id}
   params: id(path)*, invoice_date_at(query)
== pay-an-invoice: POST /invoice/{id}/pay
   params: id(path)*, apply_balance(query), email_receipt(query), payment_method_id(query), idempotency_id(query), funding(query)
== get-all-invoices: GET /invoice
   params: id(query), name(query), keywords[](query), company_name(query), email(query), address(query), price(query), schedule_id(query), payment_method(query)
== get-an-invoice: GET /invoice/{id}
   params: keyword[](query), id(path)*
== delete-an-invoice: DELETE /invoice/{id}
   params: id(path)*
== send-an-invoice-via-email: PUT /invoice/{id}/send/email
   params: id(path)*
   body: ccEmails:array
== mark-an-invoice-paid-in-cash-or-check: POST /invoice/{id}/pay/{method}
   params: id(path)*, method(path)*, apply_balance(query), email_receipt(query), check_number(query), driver_license(query), driver_license_state(query), payment_phone(query), payment_note(query)
== create-a-scheduled-invoice: POST /invoice/schedule/
   body: url*:string; total*:number; rule:string; customer_id:string; payment_method_id:string; email_notification:boolean; files:array; meta:object; meta.subtotal:number; meta.tax:number; meta.fee:number; meta.poNumber:string; meta.shippingAmount:number; meta.hideCustomerLineItems:boolean; meta.lineItems:array; meta.lineItems[].item:string; meta.lineItems[].quantity:number; meta.lineItems[].price:number
== retrieve-all-scheduled-invoices: GET /invoice/schedule
   params: customer_id(query), sort(query), order(query)
== edit-an-invoice-schedule: PUT /invoice/schedule/{id}
   params: id(path)*
   body: total:number; rule:string; active:boolean; email_notification:boolean; files:array; meta:object; meta.subtotal:number; meta.tax:number; meta.fee:number; meta.poNumber:string; meta.shippingAmount:number; meta.hideCustomerLineItems:boolean; meta.lineItems:array; meta.lineItems[].item:string; meta.lineItems[].quantity:number; meta.lineItems[].price:number
== delete-invoice-schedule: DELETE /invoice/schedule/{id}
   params: id(path)*
== get-list-of-deposits: GET /query/deposit
   params: startDate(query)*, endDate(query)*, timezone(query), timeZoneOffset(query)
== get-detail-of-specific-deposit: GET /query/depositDetail
   params: startDate(query)*, endDate(query)*, batchId(query), timezone(query), timeZoneOffset(query)
== ach-rejections: GET /query/statement/v3/ach-rejects
   params: startDate(query)*, endDate(query)*
== get-self-and-teams-info: GET /self
== team-summary: GET /query/statistics/teamSummary
```
