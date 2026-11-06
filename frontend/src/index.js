import ReactDOM from "react-dom/client";
import "./styles.css";
import App from "./App";

// No React.StrictMode: in development it mounts every component twice on purpose, and AG Studio's
// grid widget doesn't survive being created, destroyed and created again (the "Every order" table
// came up empty under `npm start` while the production build was fine).
ReactDOM.createRoot(document.getElementById("root")).render(<App />);