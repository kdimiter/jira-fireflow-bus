import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { parse } from 'yaml';
const manifest = parse(readFileSync('manifest.yml', 'utf8'));
const field = manifest.modules['jira:customField'][0];
assert.equal(field.type, 'object');
assert.equal(field.schema.properties.schemaVersion.enum[0], 1);
assert.equal(field.edit.validation, undefined,
  'manifest validation must stay disabled because Jira evaluates every global copy of the Forge field');
assert.equal(field.schema.properties.justification.minLength, 1);
assert.equal(field.schema.properties.trafficLines.minItems, 1);
assert.equal(field.schema.properties.trafficLines.maxItems, 100);
assert.deepEqual(field.edit.experience, ['issue-view', 'issue-create']);
for (const resource of manifest.resources) assert(existsSync(resource.path));
assert(field.view.formatter.expression);
console.log('Manifest structural check passed (not Forge service validation).');
if (manifest.app.id.endsWith('/00000000-0000-0000-0000-000000000000')) console.log('Registration required: placeholder app ID remains.');
