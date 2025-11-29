import { SharedLayout } from "@quartz/cfg"
import * as Component from "@quartz/components"

export const customizeSharedLayout = (config: SharedLayout): SharedLayout => {
  config.footer = Component.Footer({
    links: {
      GitHub: "https://github.com/ooMia/",
      LinkedIn: "https://www.linkedin.com/in/oomia/",
    },
  })

  return config
}
