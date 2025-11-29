import { QuartzConfig } from "@quartz/cfg"

export const customize = (config: QuartzConfig): QuartzConfig => {
  config.configuration.pageTitle = "oomia.github.io"
  config.configuration.locale = "en-US"
  config.configuration.baseUrl = "oomia.github.io"

  return config
}
