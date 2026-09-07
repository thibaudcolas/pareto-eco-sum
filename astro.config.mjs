import { defineConfig } from "astro/config";

// Static output, no integrations/adapter: the site is a fully static report
// rendered to plain HTML in dist/.
export default defineConfig({
  output: "static",
});
