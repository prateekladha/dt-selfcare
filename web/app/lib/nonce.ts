import { createContext, useContext } from "react";

/** Per-request CSP nonce (set by server.mjs, provided by entry.server.tsx). Every script React Router
 *  renders carries it; scripts those load (route chunks, Razorpay checkout) are trusted through
 *  'strict-dynamic'. In the browser the value is "" (browsers hide nonces from the DOM anyway). */
export const NonceContext = createContext<string | undefined>(undefined);
export const useNonce = () => useContext(NonceContext);
