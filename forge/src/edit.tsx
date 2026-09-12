import React, { useEffect, useRef, useState } from 'react';
import ForgeReconciler, { Box, Button, Heading, Inline, Label, SectionMessage, Select, Stack, Text, TextArea, Textfield } from '@forge/react';
import { CustomFieldEdit } from '@forge/react/jira';
import { view } from '@forge/bridge';
import { Address, AddressKind, blankLine, blankRequest, duplicateRow, MAX_ROWS, readForgeFieldContext, removeRow, RequestValue, shouldPersistDraft, TrafficLine, validateRequest } from './model';

const kinds = [{ label: 'IP-адреса', value: 'ip' }, { label: 'Hostname', value: 'hostname' },
  { label: 'Діапазон адрес', value: 'range' }, { label: 'Підмережа / маска', value: 'subnet' }];
const actions = [{ label: 'Відкрити доступ', value: 'Allow' }, { label: 'Закрити доступ', value: 'Drop' }];
const protocols = [{ label: 'TCP', value: 'tcp' }, { label: 'UDP', value: 'udp' }];
const hints: Record<AddressKind, string> = { ip: '203.0.113.15', hostname: 'app01.example.local', range: '203.0.113.10-203.0.113.30', subnet: '203.0.113.0/24' };
function AddressEditor({ id, title, value, change }: { id: string; title: string; value: Address; change: (a: Address) => void }) {
  return <Stack space="space.050">
    <Label labelFor={`${id}-kind`}>{title}: тип</Label>
    <Select inputId={`${id}-kind`} options={kinds} value={kinds.find(k => k.value === value.kind)}
      onChange={option => { if (option && !Array.isArray(option)) change({ ...value, kind: option.value as AddressKind }); }} />
    <Label labelFor={`${id}-value`}>{title}: значення</Label>
    <Textfield id={`${id}-value`} value={value.value} placeholder={hints[value.kind]}
      onChange={e => change({ ...value, value: e.target.value })} />
  </Stack>;
}
function RowEditor({ row, index, change, duplicate, remove, canDelete, canAdd }: {
  row: TrafficLine; index: number; change: (line: TrafficLine) => void;
  duplicate: () => void; remove: () => void; canDelete: boolean; canAdd: boolean;
}) {
  return <Box padding="space.150" backgroundColor="color.background.neutral.subtle">
    <Stack space="space.150">
      <Heading size="small">Доступ {index + 1}</Heading>
      <Inline space="space.200" shouldWrap>
        <AddressEditor id={`source-${index}`} title="Джерело" value={row.source} change={source => change({ ...row, source })} />
        <AddressEditor id={`destination-${index}`} title="Призначення" value={row.destination} change={destination => change({ ...row, destination })} />
      </Inline>
      {row.services.map((service, n) => <Inline key={n} space="space.100" shouldWrap>
        <Stack space="space.050">
          <Label labelFor={`protocol-${index}-${n}`}>Протокол {n + 1}</Label>
          <Select inputId={`protocol-${index}-${n}`} options={protocols} value={protocols.find(p => p.value === service.protocol)}
            onChange={option => { if (option && !Array.isArray(option)) change({ ...row, services: row.services.map((s, i) => i === n ? { ...s, protocol: option.value as 'tcp' | 'udp' } : s) }); }} />
        </Stack>
        <Stack space="space.050">
          <Label labelFor={`port-${index}-${n}`}>Порт призначення {n + 1}</Label>
          <Textfield id={`port-${index}-${n}`} type="number" min={1} max={65535} value={service.port ? String(service.port) : ''}
            onChange={e => change({ ...row, services: row.services.map((s, i) => i === n ? { ...s, port: Number(e.target.value) } : s) })} />
        </Stack>
        <Button isDisabled={row.services.length === 1} onClick={() => change({ ...row, services: row.services.filter((_, i) => i !== n) })}>Прибрати сервіс {n + 1}</Button>
      </Inline>)}
      <Inline space="space.100" shouldWrap>
        <Button isDisabled={row.services.length >= 100} onClick={() => change({ ...row, services: [...row.services, { kind: 'port', protocol: 'tcp', port: 443 }] })}>+ Додати сервіс</Button>
        <Button isDisabled={!canAdd} onClick={duplicate}>Дублювати доступ {index + 1}</Button>
        <Button isDisabled={!canDelete} onClick={remove}>Видалити доступ {index + 1}</Button>
      </Inline>
    </Stack>
  </Box>;
}
function Edit() {
  const [value, setValue] = useState<RequestValue | null>(null);
  const [loadError, setLoadError] = useState('');
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const [renderContext, setRenderContext] = useState('');
  const submitChain = useRef<Promise<void>>(Promise.resolve());
  useEffect(() => {
    let active = true;
    view.getContext().then(context => {
      if (!active) return;
      try {
        const { fieldValue: existing, renderContext: currentRenderContext } = readForgeFieldContext(context);
        setRenderContext(currentRenderContext);
        setValue(existing == null ? blankRequest() : validateRequest(existing));
      } catch {
        setLoadError('Не вдалося прочитати поле або його формат не підтримується. Оновіть сторінку чи зверніться до адміністратора; наявні дані не перезаписано.');
      }
    }).catch(() => {
      if (active) setLoadError('Не вдалося отримати контекст Jira. Оновіть сторінку чи зверніться до адміністратора.');
    });
    return () => { active = false; };
  }, []);
  const submitPayload = (payload: RequestValue) => {
    const pending = submitChain.current.then(() => view.submit(payload));
    submitChain.current = pending.catch(() => undefined);
    return pending;
  };
  useEffect(() => {
    if (!value || !shouldPersistDraft(renderContext)) return;
    let payload: RequestValue;
    try { payload = validateRequest(value); } catch { return; }
    const timer = setTimeout(() => {
      setError('');
      void submitPayload(payload).catch(e => {
        setError(e instanceof Error ? e.message : 'Не вдалося передати значення до Jira. Повторіть спробу.');
      });
    }, 200);
    return () => clearTimeout(timer);
  }, [value, renderContext]);
  if (loadError) return <SectionMessage appearance="error"><Text>{loadError}</Text></SectionMessage>;
  if (!value) return <Text>Завантаження доступів…</Text>;
  const submit = async () => {
    if (saving) return;
    try {
      const payload = validateRequest(value);
      setSaving(true); setError('');
      await submitPayload(payload);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Не вдалося зберегти. Повторіть спробу.');
      throw e;
    }
    finally { setSaving(false); }
  };
  return <CustomFieldEdit onSubmit={submit} disableSubmitOnEnter>
    <Stack space="space.200">
      <Text>Один рядок — один напрямок доступу. Діапазон або підмережа залишаються одним значенням.</Text>
      <Label labelFor="justification">Обґрунтування</Label>
      <TextArea id="justification" value={value.justification} onChange={e => setValue({ ...value, justification: e.target.value })} />
      <Label labelFor="change-type">Тип зміни</Label>
      <Select inputId="change-type" options={actions} value={actions.find(a => a.value === value.changeType)}
        onChange={option => { if (option && !Array.isArray(option)) setValue({ ...value, changeType: option.value as 'Allow' | 'Drop' }); }} />
      {value.trafficLines.map((row, index) => <RowEditor key={index} row={row} index={index}
        change={line => setValue({ ...value, trafficLines: value.trafficLines.map((r, i) => i === index ? line : r) })}
        duplicate={() => setValue(duplicateRow(value, index))} remove={() => setValue(removeRow(value, index))}
        canDelete={value.trafficLines.length > 1} canAdd={value.trafficLines.length < MAX_ROWS} />)}
      <Button isDisabled={value.trafficLines.length >= MAX_ROWS} onClick={() => setValue({ ...value, trafficLines: [...value.trafficLines, blankLine()] })}>+ Додати доступ</Button>
      {error && <SectionMessage appearance="error"><Text>{error}</Text></SectionMessage>}
    </Stack>
  </CustomFieldEdit>;
}
ForgeReconciler.render(<Edit />);
