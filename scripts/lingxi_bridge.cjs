// Project adapter: invoke the official skill, keep all credentials in its shared directory.
const path = require('path');
const fs = require('fs');
const root = path.resolve(__dirname, '../.local/vendor/gtht');
const allowed = {
  quote: ['lingxi-realtimemarketdata-skill', 'market', 'marketdata-tool'],
  financial: ['lingxi-financialsearch-skill', 'financial', 'financial-search'],
  screen: ['lingxi-smartstockselection-skill', 'financial', 'financial-search'],
  rank: ['lingxi-ranklist-skill', 'ranklist', 'ranklist'],
};
async function main() {
  const mode = process.argv[2];
  if (!allowed[mode]) throw new Error('Unsupported read-only operation');
  const config = JSON.parse(fs.readFileSync(path.join(root, 'gtht-skill-shared/gtht-entry.json'), 'utf8'));
  if (!config.apiKey) throw new Error('Authorization missing');
  const [folder, gateway, tool] = allowed[mode];
  const sdk = require(path.join(root, folder, 'skill-entry.js'));
  // Vendor output must never expose account headers or authorization data.
  console.log = () => {}; console.error = () => {};
  const args = mode === 'rank' ? {code:'BK101003',limit:40,offset:0,sorted_type:1,order_by:Number(process.argv[3] || 10),mask:{M_64_0:35184372088831}} : mode === 'quote'
    ? sdk.mcpClient.parseKvArgs(['reduced_codes=' + process.argv[3]])
    : {query: process.argv[3]};
  const result = await sdk.mcpClient.callTool(gateway, tool, args);
  let output = JSON.stringify(result);
  output = output.split(config.apiKey).join('[REDACTED]');
  process.stdout.write(output);
}
main().catch(() => {process.stdout.write(JSON.stringify({error:'灵犀请求失败，请检查授权、网络或服务状态'}));process.exitCode=1;});
