import ipaddr from 'ipaddr.js';

export type AddressKind = 'ip' | 'subnet' | 'range' | 'hostname';
export type Address = { kind: AddressKind; value: string };
export type Service = { kind: 'port'; protocol: 'tcp' | 'udp'; port: number };
export type TrafficLine = { source: Address; destination: Address; services: Service[] };
export type RequestValue = { schemaVersion: 1; justification: string; changeType: 'Allow' | 'Drop';
  duration: { kind: 'permanent' }; trafficLines: TrafficLine[] };
export const MAX_ROWS = 100;
export const blankLine = (): TrafficLine => ({ source: { kind: 'ip', value: '' },
  destination: { kind: 'ip', value: '' }, services: [{ kind: 'port', protocol: 'tcp', port: 443 }] });
export const blankRequest = (): RequestValue => ({ schemaVersion: 1, justification: '', changeType: 'Allow',
  duration: { kind: 'permanent' }, trafficLines: [blankLine()] });
export function duplicateRow(value: RequestValue, index: number): RequestValue {
  if (value.trafficLines.length >= MAX_ROWS) return value;
  const rows = [...value.trafficLines];
  rows.splice(index + 1, 0, structuredClone(rows[index]));
  return { ...value, trafficLines: rows };
}
export function removeRow(value: RequestValue, index: number): RequestValue {
  return value.trafficLines.length === 1 ? value : { ...value, trafficLines: value.trafficLines.filter((_, i) => i !== index) };
}
function address(raw: Address): Address {
  if (!raw || typeof raw.value !== 'string' || !raw.value.trim()) throw Error('Вкажіть адресу.');
  const value = raw.value.trim();
  // ipaddr accepts shorthand IPv4; require dotted decimal to match the bus.
  const parse = (s: string) => {
    if (!s.includes(':') && !/^(0|[1-9]\d{0,2})(\.(0|[1-9]\d{0,2})){3}$/.test(s)) throw Error('Невірна IP-адреса.');
    if (s.includes('%')) throw Error('Scoped IPv6 не підтримується.');
    return ipaddr.parse(s);
  };
  try {
    if (raw.kind === 'ip') parse(value);
    else if (raw.kind === 'subnet') {
      const [ip, prefix, extra] = value.split('/');
      if (extra !== undefined || !/^\d+$/.test(prefix ?? '')) throw Error();
      const parsed = parse(ip);
      const bits = parsed.kind() === 'ipv4' ? 32 : 128;
      if (Number(prefix) > bits) throw Error();
      const bytes = parsed.toByteArray();
      if (bytes.some((byte, i) => (byte & (255 >>> Math.max(0, Math.min(8, Number(prefix) - i * 8)))) !== 0)) throw Error();
    } else if (raw.kind === 'range') {
      const parts = value.split(/\s*[-–]\s*/);
      if (parts.length !== 2) throw Error();
      const first = parse(parts[0]), last = parse(parts[1]);
      if (first.kind() !== last.kind()) throw Error();
      const a = first.toByteArray(), b = last.toByteArray();
      const different = a.findIndex((n, i) => n !== b[i]);
      if (different >= 0 && a[different] > b[different]) throw Error();
    } else if (raw.kind === 'hostname') {
      if (value.length > 253 || value.replace(/\.$/, '').split('.').some(label => !/^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/.test(label))) throw Error();
    } else throw Error();
  } catch { throw Error('Перевірте тип і значення адреси; для підмережі вкажіть адресу мережі та /маску.'); }
  return { kind: raw.kind, value };
}
export function validateRequest(raw: unknown): RequestValue {
  const value = raw as RequestValue;
  if (!value || value.schemaVersion !== 1) throw Error('Непідтримувана версія даних. Зверніться до адміністратора.');
  if (typeof value.justification !== 'string' || !value.justification.trim()) throw Error('Додайте обґрунтування доступу.');
  if (!['Allow', 'Drop'].includes(value.changeType)) throw Error('Оберіть тип зміни.');
  if (value.duration && (value.duration.kind !== 'permanent' || Object.keys(value.duration).length !== 1)) throw Error('Підтримується лише постійний доступ.');
  if (!Array.isArray(value.trafficLines) || value.trafficLines.length < 1 || value.trafficLines.length > MAX_ROWS) throw Error('Потрібно від 1 до 100 доступів.');
  const trafficLines = value.trafficLines.map((line, index) => {
    try {
      if (!Array.isArray(line.services) || line.services.length < 1 || line.services.length > 100) throw Error('Потрібно від 1 до 100 сервісів.');
      const services = line.services.map(service => {
        if (!service || service.kind !== 'port' || !['tcp', 'udp'].includes(service.protocol) || !Number.isInteger(service.port) || service.port < 1 || service.port > 65535) throw Error('Вкажіть TCP/UDP і порт 1–65535.');
        return { kind: 'port' as const, protocol: service.protocol, port: service.port };
      });
      return { source: address(line.source), destination: address(line.destination), services };
    } catch (error) { throw Error(`Доступ ${index + 1}: ${(error as Error).message}`); }
  });
  return { schemaVersion: 1, justification: value.justification.trim(), changeType: value.changeType,
    duration: { kind: 'permanent' }, trafficLines };
}
