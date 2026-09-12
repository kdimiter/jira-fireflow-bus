# Jira–FireFlow Bus: короткий старт

Повний двомовний опис міститься в [головному README](../README.md), англійська версія йде
першою. Для нового Linux-сервера використовуйте готовий Docker installer з останнього релізу:

```sh
curl -fLO https://github.com/kdimiter/jira-fireflow-bus/releases/latest/download/algosec-jira-bus-latest-docker-amd64.run
curl -fLO https://github.com/kdimiter/jira-fireflow-bus/releases/latest/download/algosec-jira-bus-latest-docker-amd64.run.sha256

sha256sum -c algosec-jira-bus-latest-docker-amd64.run.sha256
sudo sh algosec-jira-bus-latest-docker-amd64.run --guided
```

`--guided` встановлює залежності й Docker, готує Node.js 22 і Forge CLI, розгортає або
повторно використовує Forge App, створює Jira Space/work type/поля, налаштовує FireFlow та
запускає готовий контейнер. Git checkout і збирання образу на сервері не потрібні.

Після встановлення:

```sh
sudo bus_conf
sudo bus_conf --refresh-certificate
sudo bus_conf --jira-sync
sudo docker logs --tail 100 algosec-jira-bus
```

Оновлення зі збереженням чинних secrets, config і state:

```sh
sudo bus_update ./algosec-jira-bus-latest-docker-amd64.run \
  ./algosec-jira-bus-latest-docker-amd64.run.sha256
```

Докладна підготовка Jira, Forge, FireFlow і чистої Linux-системи наведена в
[покроковій інструкції](DEPLOYMENT-GUIDE-uk.md).

## Архітектура

Шина не відкриває inbound endpoint. Вона опитує Jira Cloud через HTTPS, перевіряє свіжий
стан заявки та локальне approval binding, створює FireFlow request через HTTPS і повертає
status у Jira.

Окремий opt-in режим опитує нові коментарі та зміни workflow у Jira й передає їх у FireFlow
через HTTPS. Коментарі стають внутрішніми записами History, а статуси змінюються лише за явно
налаштованою мапою. Перший запуск фіксує наявну історію як початкову точку і не відтворює її.

Секрети, state, receipts і журнал не входять до Git-репозиторію або container image.
Повні шляхи та права наведені в [інструкції](DEPLOYMENT-GUIDE-uk.md).

Повторна відправка після невизначеного результату заборонена: durable operation ID,
pre-execution receipt і reconcile використовуються для відновлення без дублювання заявки.
