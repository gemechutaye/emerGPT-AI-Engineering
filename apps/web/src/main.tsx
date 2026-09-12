import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import { SharedChat } from "./SharedChat";
import "./styles.css";
import "./workspace.css";
import "./pika.css";
import "./voice.css";
import "./sidebar.css";
import "./appearance.css";
import "./craft.css";
import "./home-disco.css";
import "./interaction-polish.css";
import "./chat-polish.css";
import "./inside-emer.css";
import "./voice-experience.css";
import { initializeAppearance } from "./AppearanceMenu";

initializeAppearance();

const client = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false },
    mutations: { retry: false },
  },
});
const sharedToken = window.location.pathname.match(
  /^\/share\/([^/]+)\/?$/,
)?.[1];
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={client}>
      {sharedToken ? <SharedChat token={sharedToken} /> : <App />}
    </QueryClientProvider>
  </React.StrictMode>,
);
