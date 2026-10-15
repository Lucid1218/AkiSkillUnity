using System.Collections.Generic;
using UnityEngine;

[CreateAssetMenu(fileName = "OutfitRetargetingSettings", menuName = "MochiFitter/Outfit Retargeting Settings")]
public class OutfitRetargetingSettings : ScriptableObject
{
    [Header("Avatar Settings")]
    public GameObject targetPrefab;
    public UnityEngine.Object baseFbxAsset;
    public TextAsset configJsonAsset;
    
    [Header("Conversion Settings")]
    public int selectedTargetAvatarIndex = 0;
    public int selectedSourceAvatarIndex = 0;
    public string selectedTargetAvatarName = "";
    public string selectedSourceAvatarName = "";
    
    [Header("Blend Shape Settings")]
    public List<string> blendShapeSelectionKeys = new List<string>();
    public List<bool> blendShapeSelectionValues = new List<bool>();
    public List<string> blendShapeValueKeys = new List<string>();
    public List<float> blendShapeValueValues = new List<float>();
    
    [Header("Other Settings")]
    public bool enableSubdivision = false;
    public bool disableTriangulation = false;
    public bool useOmochiFormat = false;
    public string outputFolderName = "Outputs";
    public string tempFolderName = "temp";
    
    // Dictionary用のヘルパーメソッド
    public Dictionary<string, bool> GetBlendShapeSelections()
    {
        var dict = new Dictionary<string, bool>();
        for (int i = 0; i < blendShapeSelectionKeys.Count && i < blendShapeSelectionValues.Count; i++)
        {
            dict[blendShapeSelectionKeys[i]] = blendShapeSelectionValues[i];
        }
        return dict;
    }
    
    public void SetBlendShapeSelections(Dictionary<string, bool> selections)
    {
        blendShapeSelectionKeys.Clear();
        blendShapeSelectionValues.Clear();
        
        foreach (var kvp in selections)
        {
            blendShapeSelectionKeys.Add(kvp.Key);
            blendShapeSelectionValues.Add(kvp.Value);
        }
    }
    
    public Dictionary<string, float> GetBlendShapeValues()
    {
        var dict = new Dictionary<string, float>();
        for (int i = 0; i < blendShapeValueKeys.Count && i < blendShapeValueValues.Count; i++)
        {
            dict[blendShapeValueKeys[i]] = blendShapeValueValues[i];
        }
        return dict;
    }
    
    public void SetBlendShapeValues(Dictionary<string, float> values)
    {
        blendShapeValueKeys.Clear();
        blendShapeValueValues.Clear();
        
        foreach (var kvp in values)
        {
            blendShapeValueKeys.Add(kvp.Key);
            blendShapeValueValues.Add(kvp.Value);
        }
    }
}
