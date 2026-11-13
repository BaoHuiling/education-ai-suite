import React, { useEffect, useId, useRef, useState } from "react";
import "../../assets/css/Accordion.css";

interface AccordionProps {
  title: string;
  children: React.ReactNode;
}

/** Keep in sync with --accordion-duration in Accordion.css. */
const ANIMATION_MS = 260;

const Accordion: React.FC<AccordionProps> = ({ title, children }) => {
  const [isOpen, setIsOpen] = useState(true);
  // Content is only mounted while open (as before), but stays mounted for the
  // length of the close animation so it can slide shut instead of vanishing.
  const [isMounted, setIsMounted] = useState(true);
  const closeTimer = useRef<number | null>(null);
  const id = useId();
  const headerId = `${id}-header`;
  const panelId = `${id}-panel`;

  const clearCloseTimer = () => {
    if (closeTimer.current !== null) {
      window.clearTimeout(closeTimer.current);
      closeTimer.current = null;
    }
  };

  useEffect(() => clearCloseTimer, []);

  const toggle = () => {
    clearCloseTimer();
    if (isOpen) {
      setIsOpen(false);
      closeTimer.current = window.setTimeout(() => setIsMounted(false), ANIMATION_MS);
    } else {
      setIsMounted(true);
      setIsOpen(true);
    }
  };

  return (
    <section className={`accordion${isOpen ? " is-open" : ""}`}>
      <h3 className="accordion-heading">
        <button
          type="button"
          id={headerId}
          className="accordion-header"
          aria-expanded={isOpen}
          aria-controls={panelId}
          onClick={toggle}
        >
          <span className="accordion-title">{title}</span>
          <span className="accordion-chevron" aria-hidden="true">
            <svg viewBox="0 0 16 16" width="16" height="16" fill="none">
              <path d="M6 3.5 10.5 8 6 12.5" stroke="currentColor" strokeWidth="1.75"
                strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </span>
        </button>
      </h3>

      {isMounted && (
        <div
          id={panelId}
          role="region"
          aria-labelledby={headerId}
          className="accordion-panel"
          inert={!isOpen}
        >
          <div className="accordion-panel__inner">
            <div className="accordion-content">{children}</div>
          </div>
        </div>
      )}
    </section>
  );
};

export default Accordion;
