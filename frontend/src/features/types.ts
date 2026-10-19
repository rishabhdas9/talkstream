import type { ReactElement } from "react";

import type { PublicCapabilities } from "../api/capabilities";

export type AddonPageProps = {
  token: string;
  pushToast: (message: string) => void;
  onAuthenticationExpired: () => void;
};

export type FrontendAddon = {
  id: string;
  label: string;
  icon: ReactElement;
  isEnabled: (capabilities: PublicCapabilities | undefined) => boolean;
  render: (props: AddonPageProps) => ReactElement;
};
