// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import React from 'react';

export type ButtonVariant = 'primary' | 'secondary' | 'danger' | 'link';

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
}

/** The one button of the settings screens and the setup assistant. */
const Button: React.FC<ButtonProps> = ({ variant = 'secondary', className, type = 'button', ...rest }) => (
  <button
    type={type}
    className={['ui-btn', variant !== 'secondary' && `ui-btn-${variant}`, className].filter(Boolean).join(' ')}
    {...rest}
  />
);

export default Button;
