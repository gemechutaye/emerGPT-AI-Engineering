import { Icon } from "./Icon";

export function Brand({ navigation = false }: { navigation?: boolean }) {
  return (
    <>
      <span className="brand-art" aria-hidden="true">
        <span className="brand-symbol">
          <i />
          <i />
          <i />
        </span>
        {navigation && (
          <span className="brand-menu">
            <Icon name="sidebar" size={22} />
          </span>
        )}
      </span>
      <span className="brand-wordmark">
        emer<span className="brand-suffix">-GPT</span>
      </span>
    </>
  );
}
