import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Boundary } from "../Boundary";
import { WorkspaceApp } from "./WorkspaceApp";
import "../styles.css";
import "./workspace.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Boundary>
      <WorkspaceApp />
    </Boundary>
  </StrictMode>,
);
