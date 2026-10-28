import SwiftUI

/// Available visual themes for the XASS iOS application.
public enum AppTheme: String, CaseIterable, Identifiable, Codable, Sendable {
    case cyber = "cyber"
    case tokyo = "tokyo"
    case nordic = "nordic"
    case matrix = "matrix"
    case sunset = "sunset"
    case glass = "glass"

    public var id: String { rawValue }

    public var displayName: String {
        switch self {
        case .cyber: return "Cyber"
        case .tokyo: return "Tokyo Night"
        case .nordic: return "Nordic Frost"
        case .matrix: return "Matrix"
        case .sunset: return "Sunset"
        case .glass: return "Liquid Glass"
        }
    }

    public var localizedDescription: String {
        switch self {
        case .cyber: return "Неоновый синий и глубокий технологичный фон"
        case .tokyo: return "Ночной пурпурный и фуксия на фоне ночного индиго"
        case .nordic: return "Минималистичный ледяной бирюзовый и арктический сланец"
        case .matrix: return "Фосфорный зелёный терминал на глубоком чёрном фоне"
        case .sunset: return "Тёплый янтарно-оранжевый закат и тёмный графит"
        case .glass: return "Полупрозрачные стеклянные панели с призматическим индиго"
        }
    }

    public var systemIcon: String {
        switch self {
        case .cyber: return "bolt.fill"
        case .tokyo: return "sparkles"
        case .nordic: return "snowflake"
        case .matrix: return "terminal.fill"
        case .sunset: return "sunset.fill"
        case .glass: return "drop.fill"
        }
    }

    // MARK: - Color Palette

    public var backgroundColor: Color {
        switch self {
        case .cyber:
            return Color(red: 10 / 255, green: 13 / 255, blue: 20 / 255)
        case .tokyo:
            return Color(red: 16 / 255, green: 16 / 255, blue: 28 / 255)
        case .nordic:
            return Color(red: 15 / 255, green: 20 / 255, blue: 26 / 255)
        case .matrix:
            return Color(red: 5 / 255, green: 8 / 255, blue: 5 / 255)
        case .sunset:
            return Color(red: 20 / 255, green: 14 / 255, blue: 14 / 255)
        case .glass:
            return Color(red: 11 / 255, green: 15 / 255, blue: 25 / 255)
        }
    }

    public var cardColor: Color {
        switch self {
        case .cyber:
            return Color(red: 21 / 255, green: 27 / 255, blue: 38 / 255)
        case .tokyo:
            return Color(red: 26 / 255, green: 26 / 255, blue: 46 / 255)
        case .nordic:
            return Color(red: 30 / 255, green: 38 / 255, blue: 49 / 255)
        case .matrix:
            return Color(red: 14 / 255, green: 25 / 255, blue: 16 / 255)
        case .sunset:
            return Color(red: 38 / 255, green: 26 / 255, blue: 25 / 255)
        case .glass:
            return Color(red: 24 / 255, green: 32 / 255, blue: 50 / 255).opacity(0.7)
        }
    }

    public var accentColor: Color {
        switch self {
        case .cyber:
            return Color(red: 0 / 255, green: 210 / 255, blue: 255 / 255)
        case .tokyo:
            return Color(red: 247 / 255, green: 37 / 255, blue: 133 / 255)
        case .nordic:
            return Color(red: 72 / 255, green: 202 / 255, blue: 228 / 255)
        case .matrix:
            return Color(red: 0 / 255, green: 255 / 255, blue: 102 / 255)
        case .sunset:
            return Color(red: 255 / 255, green: 107 / 255, blue: 53 / 255)
        case .glass:
            return Color(red: 99 / 255, green: 102 / 255, blue: 241 / 255)
        }
    }

    public var glowingTintColor: Color {
        switch self {
        case .cyber:
            return Color(red: 0 / 255, green: 210 / 255, blue: 255 / 255).opacity(0.35)
        case .tokyo:
            return Color(red: 247 / 255, green: 37 / 255, blue: 133 / 255).opacity(0.35)
        case .nordic:
            return Color(red: 72 / 255, green: 202 / 255, blue: 228 / 255).opacity(0.30)
        case .matrix:
            return Color(red: 0 / 255, green: 255 / 255, blue: 102 / 255).opacity(0.35)
        case .sunset:
            return Color(red: 255 / 255, green: 107 / 255, blue: 53 / 255).opacity(0.35)
        case .glass:
            return Color(red: 99 / 255, green: 102 / 255, blue: 241 / 255).opacity(0.35)
        }
    }

    public var secondaryTextColor: Color {
        switch self {
        case .cyber:
            return Color(red: 148 / 255, green: 163 / 255, blue: 184 / 255)
        case .tokyo:
            return Color(red: 180 / 255, green: 170 / 255, blue: 205 / 255)
        case .nordic:
            return Color(red: 154 / 255, green: 170 / 255, blue: 186 / 255)
        case .matrix:
            return Color(red: 120 / 255, green: 170 / 255, blue: 130 / 255)
        case .sunset:
            return Color(red: 195 / 255, green: 165 / 255, blue: 155 / 255)
        case .glass:
            return Color(red: 165 / 255, green: 175 / 255, blue: 195 / 255)
        }
    }

    public var borderColor: Color {
        switch self {
        case .cyber:
            return accentColor.opacity(0.25)
        case .tokyo:
            return accentColor.opacity(0.3)
        case .nordic:
            return accentColor.opacity(0.2)
        case .matrix:
            return accentColor.opacity(0.3)
        case .sunset:
            return accentColor.opacity(0.25)
        case .glass:
            return Color.white.opacity(0.18)
        }
    }

    // MARK: - VoiceOver Accessibility

    public var accessibilityLabel: String {
        "Тема \(displayName). \(localizedDescription)"
    }

    public var accessibilityHint: String {
        "Дважды коснитесь, чтобы выбрать тему оформления \(displayName)"
    }
}

// MARK: - Color Theme Extensions

extension Color {
    public static func themeBackground(for theme: AppTheme) -> Color { theme.backgroundColor }
    public static func themeCard(for theme: AppTheme) -> Color { theme.cardColor }
    public static func themeAccent(for theme: AppTheme) -> Color { theme.accentColor }
    public static func themeGlow(for theme: AppTheme) -> Color { theme.glowingTintColor }
    public static func themeSecondary(for theme: AppTheme) -> Color { theme.secondaryTextColor }
}

// MARK: - Theme Manager

@MainActor
public final class ThemeManager: ObservableObject {
    public static let shared = ThemeManager()

    @AppStorage("app_theme") public var currentTheme: AppTheme = .cyber {
        didSet {
            objectWillChange.send()
        }
    }

    public init() {}

    public func setTheme(_ theme: AppTheme) {
        currentTheme = theme
    }
}

// MARK: - SwiftUI Environment Key

private struct AppThemeKey: EnvironmentKey {
    static let defaultValue: AppTheme = .cyber
}

extension EnvironmentValues {
    public var appTheme: AppTheme {
        get { self[AppThemeKey.self] }
        set { self[AppThemeKey.self] = newValue }
    }
}

// MARK: - Theme UI Views

/// Visual card for a single theme selection.
public struct ThemeCardView: View {
    public let theme: AppTheme
    public let isSelected: Bool
    public let onSelect: () -> Void

    public init(theme: AppTheme, isSelected: Bool, onSelect: @escaping () -> Void) {
        self.theme = theme
        self.isSelected = isSelected
        self.onSelect = onSelect
    }

    public var body: some View {
        Button(action: onSelect) {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    ZStack {
                        Circle()
                            .fill(theme.cardColor)
                            .frame(width: 36, height: 36)
                            .overlay(Circle().stroke(theme.borderColor, lineWidth: 1.5))
                        Image(systemName: theme.systemIcon)
                            .font(.system(size: 16, weight: .semibold))
                            .foregroundStyle(theme.accentColor)
                    }

                    Spacer()

                    if isSelected {
                        Image(systemName: "checkmark.circle.fill")
                            .font(.title3)
                            .foregroundStyle(theme.accentColor)
                            .shadow(color: theme.glowingTintColor, radius: 4)
                    }
                }

                VStack(alignment: .leading, spacing: 3) {
                    Text(theme.displayName)
                        .font(.headline.weight(.semibold))
                        .foregroundStyle(.white)

                    Text(theme.localizedDescription)
                        .font(.caption)
                        .foregroundStyle(theme.secondaryTextColor)
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                }

                // Palette preview swatches
                HStack(spacing: 6) {
                    Circle().fill(theme.backgroundColor).frame(width: 14, height: 14)
                        .overlay(Circle().stroke(Color.white.opacity(0.2), lineWidth: 0.5))
                    Circle().fill(theme.cardColor).frame(width: 14, height: 14)
                    Circle().fill(theme.accentColor).frame(width: 14, height: 14)
                    Circle().fill(theme.glowingTintColor).frame(width: 14, height: 14)
                    Spacer()
                }
                .padding(.top, 4)
            }
            .padding(14)
            .background(
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .fill(theme.cardColor)
                    .overlay(
                        RoundedRectangle(cornerRadius: 16, style: .continuous)
                            .stroke(isSelected ? theme.accentColor : theme.borderColor, lineWidth: isSelected ? 2 : 1)
                    )
                    .shadow(color: isSelected ? theme.glowingTintColor : Color.clear, radius: 8)
            )
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(theme.accessibilityLabel)
        .accessibilityValue(isSelected ? "Выбрана" : "Не выбрана")
        .accessibilityHint(theme.accessibilityHint)
        .accessibilityAddTraits(isSelected ? [.isButton, .isSelected] : [.isButton])
    }
}

/// Selector sheet or view containing all theme options in a grid.
public struct ThemeSelectorView: View {
    @ObservedObject var themeManager = ThemeManager.shared
    @Environment(\.dismiss) private var dismiss

    public init() {}

    private let columns = [
        GridItem(.adaptive(minimum: 155, maximum: 200), spacing: 14)
    ]

    public var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Темы оформления")
                        .font(.title2.weight(.bold))
                        .foregroundStyle(.white)

                    Text("Выберите палитру интерфейса XASS. Тема применяется ко всем вкладкам, виджетам и агенту Hermes.")
                        .font(.subheadline)
                        .foregroundStyle(themeManager.currentTheme.secondaryTextColor)
                }
                .padding(.horizontal, 16)
                .padding(.top, 10)

                LazyVGrid(columns: columns, spacing: 14) {
                    ForEach(AppTheme.allCases) { theme in
                        ThemeCardView(
                            theme: theme,
                            isSelected: themeManager.currentTheme == theme,
                            onSelect: {
                                withAnimation(.spring(response: 0.35, dampingFraction: 0.8)) {
                                    themeManager.setTheme(theme)
                                }
                            }
                        )
                    }
                }
                .padding(.horizontal, 16)
            }
            .padding(.vertical, 16)
        }
        .background(themeManager.currentTheme.backgroundColor.ignoresSafeArea())
        .navigationTitle("Оформление")
        .navigationBarTitleDisplayMode(.inline)
    }
}
