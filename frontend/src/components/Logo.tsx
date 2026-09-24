"use client";

type Props = {
  size?: number;
  className?: string;
};

export default function Logo({ size = 48, className = "" }: Props) {
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src="/logo.png"
      alt="My Booth Agent"
      width={size}
      height={size}
      className={`object-contain ${className}`}
      style={{ width: size, height: size }}
    />
  );
}
