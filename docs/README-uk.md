# Архітектура

Шина не відкриває inbound endpoint. Вона опитує Jira Cloud через HTTPS, перевіряє свіжий
стан заявки та локальне approval binding, створює FireFlow request через HTTPS і повертає
status у Jira.

Окремий opt-in режим опитує нові коментарі та зміни workflow у Jira й передає їх у FireFlow
через HTTPS. Коментарі стають внутрішніми записами History, а статуси змінюються лише за явно
налаштованою мапою. Перший запуск фіксує наявну історію як початкову точку і не відтворює її.

Секрети, state, receipts і журнал не входять до Git-репозиторію або container image.
Повні шляхи та права наведені в [інструкції](DEPLOYMENT-GUIDE-uk.md).
Підтримувані шаблони, їхні стандартні workflow, межі REST API та порядок безпечного запуску
Automatic Traffic Change наведені в [окремому довіднику](FIREFLOW-TEMPLATES-AND-WORKFLOWS-uk.md).

Повторна відправка після невизначеного результату заборонена: durable operation ID,
pre-execution receipt і reconcile використовуються для відновлення без дублювання заявки.
