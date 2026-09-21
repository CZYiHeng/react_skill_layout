import { build } from 'vite'

await build({
  root: 'g:/react-agent/web',
  configFile: 'g:/react-agent/web/vite.config.js',
  minify: true,
  sourcemap: true,
  cache: false,
  logLevel: 'info',
})
