// Read package metadata only. Never import installed package code or run hooks.
const fs = require('node:fs');
const semver = require('/usr/local/lib/node_modules/npm/node_modules/semver');
const artifacts = JSON.parse(fs.readFileSync('/artifacts/artifacts.json', 'utf8'));
const installed = new Map(artifacts.filter(a => a.ecosystem === 'npm').map(a => [a.name, a.version]));
for (const [name] of installed) {
  const metadata = JSON.parse(fs.readFileSync(`/project/node_modules/${name}/package.json`, 'utf8'));
  for (const [kind, dependencies] of Object.entries({
    dependencies: metadata.dependencies || {},
    optionalDependencies: metadata.optionalDependencies || {},
    peerDependencies: metadata.peerDependencies || {},
  })) {
    for (const [dependency, range] of Object.entries(dependencies)) {
      const optional = kind === 'optionalDependencies'
        || (kind === 'dependencies' && dependency in (metadata.optionalDependencies || {}))
        || (kind === 'peerDependencies' && metadata.peerDependenciesMeta?.[dependency]?.optional);
      const version = installed.get(dependency);
      if (!version && optional) continue;
      if (!version || typeof range !== 'string' || !semver.validRange(range)
        || !semver.satisfies(version, range)) {
        throw new Error(`${name} requires approved ${dependency}@${range}; found ${version || 'missing'}`);
      }
    }
  }
}
console.log('Approved npm dependency closure verified without lifecycle scripts');
