// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import React from 'react';
import { useTranslation } from 'react-i18next';
import type { ConfigChange, ConfigProblem } from '../../types/config';
import { Banner, Button } from '../ui';

interface ProblemBannersProps {
  /** One per rule (useConfigProblems' `messages`). */
  messages: ConfigProblem[];
  canApply: (fix: ConfigChange[]) => boolean;
  onApplyFix: (fix: ConfigChange[]) => void;
  /** More buttons on each banner, such as "Show setting". */
  extraActions?: (problem: ConfigProblem) => React.ReactNode;
}

/** Contradictions in the settings, each with its one-click fix where there is one. */
const ProblemBanners: React.FC<ProblemBannersProps> = ({ messages, canApply, onApplyFix, extraActions }) => {
  const { t } = useTranslation();
  return (
    <>
      {messages.map((problem) => {
        const fixable = !!problem.fix?.length && canApply(problem.fix);
        const extra = extraActions?.(problem);
        return (
          <Banner
            key={problem.rule}
            tone={problem.blocking ? 'error' : 'warning'}
            actions={
              fixable || extra ? (
                <>
                  {fixable && (
                    <Button variant="primary" onClick={() => onApplyFix(problem.fix!)}>
                      {t('config.applyFix', 'Apply fix')}
                    </Button>
                  )}
                  {extra}
                </>
              ) : undefined
            }
          >
            {problem.message}
          </Banner>
        );
      })}
    </>
  );
};

export default ProblemBanners;
