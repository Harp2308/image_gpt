"""
Simple test for Azure Search client.
Tests connection and lists available collections (indexes).
"""

from colep_ai.database.azure_search_client import get_index_client, INDEX_NAME


def test_connection():
    """Test connection to Azure Search and list indexes."""
    try:
        client = get_index_client()
        print("✓ Connection successful")
        
        # List all indexes (collections)
        indexes = client.list_indexes()
        index_names = [idx.name for idx in indexes]
        
        print(f"\nAvailable indexes ({len(index_names)}):")
        for name in index_names:
            marker = "← target index" if name == INDEX_NAME else ""
            print(f"  - {name} {marker}")
        
        # Check if target index exists
        if INDEX_NAME in index_names:
            print(f"\n✓ Target index '{INDEX_NAME}' exists")
        else:
            print(f"\n✗ Target index '{INDEX_NAME}' not found")
        
        return True
        
    except Exception as e:
        print(f"✗ Connection failed: {e}")
        return False


if __name__ == "__main__":
    print("Testing Azure Search connection...")
    print("-" * 50)
    test_connection()
