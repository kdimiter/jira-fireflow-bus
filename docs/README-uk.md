# Архітектура

Шина не відкриває inbound endpoint. Вона опитує Jira Cloud через HTTPS, перевіряє свіжий
стан заявки та локальне approval binding, створює FireFlow request через HTTPS і повертає
status у Jira.

Секрети, state, receipts і журнал не входять до Git-репозиторію або container image.
Повні шляхи та права наведені в [інструкції](DEPLOYMENT-GUIDE-uk.md).

Повторна відправка після невизначеного результату заборонена: durable operation ID,
pre-execution receipt і reconcile використовуються для відновлення без дублювання заявки.
