import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App as LegacyApp } from "./legacy/App";
import { App as NewApp } from "./ui/App";

// VITE_UI=legacy boots the previous plain UI as a fallback; default is new.
const App = import.meta.env.VITE_UI === "legacy" ? LegacyApp : NewApp;

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
