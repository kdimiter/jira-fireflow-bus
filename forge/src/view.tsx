import React from 'react';
import ForgeReconciler, { DynamicTable, SectionMessage, Stack, Text, useProductContext } from '@forge/react';
import { readForgeFieldContext, validateRequest } from './model';
function View() {
  const context = useProductContext();
  if (!context) return <Text>Завантаження доступів…</Text>;
  const raw = readForgeFieldContext(context).fieldValue;
  if (raw == null) return <Text>Доступи ще не додано.</Text>;
  try {
    const value = validateRequest(raw);
    return <Stack space="space.150">
      <Text>{value.changeType === 'Allow' ? 'Відкрити доступ' : 'Закрити доступ'} · Постійний</Text>
      <Text>{value.justification}</Text>
      <DynamicTable caption="Мережеві доступи" head={{ cells: [
        { key: 'source', content: 'Джерело' }, { key: 'destination', content: 'Призначення' }, { key: 'services', content: 'Сервіси' },
      ] }} rows={value.trafficLines.map((row, i) => ({ key: String(i), cells: [
        { key: `s${i}`, content: <Text>{row.source.value}</Text> },
        { key: `d${i}`, content: <Text>{row.destination.value}</Text> },
        { key: `p${i}`, content: <Text>{row.services.map(s => `${s.protocol.toUpperCase()}/${s.port}`).join(', ')}</Text> },
      ] }))} rowsPerPage={10} />
    </Stack>;
  } catch { return <SectionMessage appearance="error"><Text>Формат даних не підтримується. Зверніться до адміністратора.</Text></SectionMessage>; }
}
ForgeReconciler.render(<View />);
